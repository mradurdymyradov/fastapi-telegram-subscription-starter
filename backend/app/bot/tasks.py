"""Periodic background jobs run inside the bot process."""
from __future__ import annotations

import datetime as _dt
import logging

from aiogram import Bot

from app.bot.keyboards import usdt_renewal_keyboard
from app.config import get_settings
from app.db.session import async_session
from app.observability import (
    HEARTBEAT_TTL_SECONDS,
    record_bot_heartbeat,
    send_ops_alert,
)
from app.services.backup_verification import (
    alert_text as backup_alert_text,
)
from app.services.backup_verification import (
    assess as assess_backup_health,
)
from app.services.backup_verification import (
    latest_verification,
    next_alert_key,
)
from app.services.billing_notifications import notify_usdt_expiring
from app.services.channel_access import (
    SubscriptionAccessRevokeResult,
    revoke_subscription_access,
)
from app.services.reconciliation import reconciliation_summary_text, run_reconciliation
from app.services.subscription import (
    expire_subscriptions,
    expiring_manual_usdt,
    record_access_revoke_attempt,
    subscriptions_pending_ban_revoke,
)
from app.services.subscription_cancellation import (
    manual_cancellation_digest_text,
    open_manual_cancellations,
)
from app.services.vimeo_sync import sync_archive

logger = logging.getLogger(__name__)

#: What an expiry removal actually says to the member, after the ban+unban.
#: Named rather than left inline so `app.ops.expiry_removal_gate` can show an
#: operator the real sentence before they arm this job, instead of quoting a
#: second copy that drifts away from the one that gets sent (GK-484).
EXPIRY_REMOVAL_DM = (
    "Ваша подписка закончилась, доступ в сообщество приостановлен. "
    "Чтобы вернуться — /subscribe."
)


def _prelaunch_hold() -> bool:
    """GK-443: is the bot meant to be inert toward members right now?

    Read at call time, not import time, so the answer follows the deployment
    rather than the moment the process happened to start.
    """
    return bool(get_settings().enable_prelaunch_hold)


def _removal_target_chat_ids(settings) -> frozenset[int]:
    """The chats an hourly removal would actually ban a member out of.

    Kept next to the check that uses it rather than derived from
    `configured_access_resources()`, which reads its own module-level settings
    snapshot and so cannot be steered per call. `test_expiry_removal_arming.py`
    asserts the two agree, so adding a third Telegram resource fails a test here
    instead of quietly escaping the binding.

    Unset ids (`0`) are dropped: `kick_user` cannot reach a resource with no
    chat id, so requiring the operator to name one would mean naming a room
    nobody is removed from.
    """
    return frozenset(
        chat_id
        for chat_id in (settings.private_channel_id, settings.practice_chat_id)
        if chat_id
    )


def _expiry_removals_refusal(settings) -> str | None:
    """GK-460: why the hourly expiry sweep must not run. ``None`` means it may.

    One boolean used to stand between a `.env` edit and an unattended ban+unban
    against whatever the config pointed at: lifting GK-443's hold armed removals
    as a side effect, on the same line, in the same action. `app.ops.cutover`
    already refuses that shape — it wants an exact token **and** an
    `--expect-chats` naming every resource a removal reaches before it removes
    anybody. This is the same second sentence for the job that does it on a
    timer. (The plural is GK-470, and it came *from* here: the cutover command
    confirmed one chat while emptying two until it was brought up to the shape
    below.)

    The binding is set equality rather than membership, so both directions are
    caught: a chat that was named and then repointed, and a chat that appeared
    without ever being named.
    """
    if not settings.enable_expiry_removals:
        return "ENABLE_EXPIRY_REMOVALS is not set — expiry removals are not armed"
    expected = settings.expiry_removals_expect_chat_ids
    target = _removal_target_chat_ids(settings)
    if not target:
        return (
            "no Telegram resource is configured, so there is nothing to remove "
            "anyone from (PRIVATE_CHANNEL_ID / PRACTICE_CHAT_ID are unset)"
        )
    if not expected:
        return (
            "ENABLE_EXPIRY_REMOVALS is on but EXPIRY_REMOVALS_EXPECT_CHATS names "
            f"no chat — set it to {','.join(str(i) for i in sorted(target))} to "
            "confirm those are the communities you mean"
        )
    if expected != target:
        return (
            "EXPIRY_REMOVALS_EXPECT_CHATS names "
            f"{sorted(expected)} but a removal would reach {sorted(target)} — "
            "refusing to act on chats nobody named"
        )
    return None


async def _alert_expiry_removals_refused(reason: str) -> None:
    """Say once a day that removals are armed and still not running (GK-460).

    Only for the armed case. A flag that is simply off is the intended state for
    the whole pre-launch window, and alerting hourly about an intended state is
    how an alert channel becomes something people scroll past — the same
    reasoning GK-436 uses for keeping `ACCESS_START_FLOOR` out of must-declare.
    An operator who set the flag, however, believes removals are happening, and
    the gap between that belief and the truth is exactly what an alert is for.
    """
    try:
        await send_ops_alert(
            "Почасовое удаление по истечении подписки не выполняется: "
            "ENABLE_EXPIRY_REMOVALS включён, но привязка к чатам не совпадает.\n"
            f"Причина: {reason}\n"
            "Никто не удалён и не будет удалён, пока это не исправлено (GK-460).",
            key="expiry_removals_refused",
            rate_limit_seconds=86_400,
            severity="error",
        )
    except Exception:
        logger.exception("failed to alert refused expiry removals")


async def kick_expired_job(bot: Bot) -> None:
    """Every hour: revoke access whose provider/local access window has ended."""
    if _prelaunch_hold():
        # GK-443. This job is the loudest thing the bot does to a member: it
        # ban+unbans them out of the channel and then DMs «Ваша подписка
        # закончилась… Чтобы вернуться — /subscribe» — a subscription pitch,
        # which is the one sentence the hold exists to prevent. A hold that
        # silences replies but lets this run at the top of the hour is the
        # 10 August incident on a timer.
        logger.info("kick_expired_job: skipped, prelaunch hold is on (GK-443)")
        return
    settings = get_settings()
    # GK-460. The refusal stops the *expiry* sweep only. The ban-retry drain
    # below keeps running deliberately: it converges a removal an administrator
    # already ordered by hand in the panel — where the immediate Telegram
    # attempt is made with no such gate — so holding it here would not prevent a
    # removal, it would only leave a failed one silently unenforced.
    refusal = _expiry_removals_refusal(settings)
    async with async_session() as session:
        expired = [] if refusal else await expire_subscriptions(session)
        seen: set = set()
        kicked = 0
        rate_limited = 0
        abandoned = 0
        for sub in expired:
            seen.add(sub.id)
            # GK-432: one member the bot cannot remove must not cost the other
            # ninety-nine their removal. Everything per-member is inside the
            # guard, including the commit and the alert.
            try:
                if not getattr(sub, "user", None):
                    await session.refresh(sub, ["user"])
                try:
                    result = await revoke_subscription_access(
                        bot,
                        sub.user.tg_id,
                        getattr(sub, "invite_link", None),
                    )
                except Exception as e:
                    logger.exception("kick_expired_job revoke crashed for subscription %s", sub.id)
                    result = SubscriptionAccessRevokeResult(False, error=str(e))
                gave_up = record_access_revoke_attempt(
                    sub,
                    success=result.success,
                    retry_after_seconds=result.retry_after,
                    error=result.error,
                    permanent=result.permanent,
                )
                if result.success:
                    kicked += 1
                    try:
                        await bot.send_message(sub.user.tg_id, EXPIRY_REMOVAL_DM)
                    except Exception:
                        pass
                elif result.retry_after:
                    rate_limited += 1
                await session.commit()
                if gave_up:
                    abandoned += 1
                    await _alert_access_revoke_abandoned(sub, result)
            except Exception:
                logger.exception("kick_expired_job failed for subscription %s", sub.id)
                await session.rollback()

        # GK-400: durable retry net for banned members. The admin ban action
        # attempts the Telegram revoke immediately; this re-attempts any that
        # failed or were never run. A ban keeps the paid subscription intact
        # (mark_status_expired=False) and sends no "subscription ended" notice.
        banned = await subscriptions_pending_ban_revoke(session)
        banned_revoked = 0
        for sub in banned:
            if sub.id in seen:
                continue
            seen.add(sub.id)
            try:
                if not getattr(sub, "user", None):
                    await session.refresh(sub, ["user"])
                try:
                    result = await revoke_subscription_access(
                        bot,
                        sub.user.tg_id,
                        getattr(sub, "invite_link", None),
                    )
                except Exception as e:
                    logger.exception(
                        "kick_expired_job ban revoke crashed for subscription %s", sub.id
                    )
                    result = SubscriptionAccessRevokeResult(False, error=str(e))
                gave_up = record_access_revoke_attempt(
                    sub,
                    success=result.success,
                    retry_after_seconds=result.retry_after,
                    error=result.error,
                    mark_status_expired=False,
                    permanent=result.permanent,
                )
                if result.success:
                    banned_revoked += 1
                await session.commit()
                if gave_up:
                    abandoned += 1
                    await _alert_access_revoke_abandoned(sub, result, banned=True)
            except Exception:
                logger.exception("kick_expired_job ban revoke failed for subscription %s", sub.id)
                await session.rollback()
    logger.info(
        "kick_expired_job: %d due, %d revoked, %d rate-limited, %d banned revoked, "
        "%d abandoned%s",
        len(expired),
        kicked,
        rate_limited,
        banned_revoked,
        abandoned,
        f" — expiry removals not run: {refusal}" if refusal else "",
    )
    if refusal and settings.enable_expiry_removals:
        await _alert_expiry_removals_refused(refusal)


async def _alert_access_revoke_abandoned(
    sub,
    result: SubscriptionAccessRevokeResult,
    *,
    banned: bool = False,
) -> None:
    """GK-432: say once, out loud, that a member could not be removed.

    Once per subscription, because the row is never selected again after this —
    the alert key is a second belt in case a human clears the terminal state.
    The member is still inside the Telegram resources; entitlement is already
    closed, so this is a Telegram-side fact for a person to act on, not an
    access leak the code can fix.
    """
    tg_id = getattr(getattr(sub, "user", None), "tg_id", None)
    reason = "Telegram refuses permanently" if result.permanent else "retry limit reached"
    context = "banned member" if banned else "expired subscription"
    try:
        await send_ops_alert(
            f"Не удалось удалить участника из Telegram — бот прекратил попытки.\n"
            f"subscription #{getattr(sub, 'id', '?')} ({context}), tg_id {tg_id}\n"
            f"Причина: {reason}. Ошибка: {result.error or 'нет текста'}\n"
            f"Попыток: {getattr(sub, 'access_revoke_attempts', '?')}. "
            f"Участник остаётся в канале/чате, доступ в портал уже закрыт. "
            f"Удалите вручную или снимите с него права.",
            key=f"access_revoke_abandoned_{getattr(sub, 'id', 'unknown')}",
            rate_limit_seconds=86_400,
            severity="error",
        )
    except Exception:
        logger.exception("failed to alert abandoned revoke for subscription %s", sub.id)


async def remind_expiring_job() -> None:
    """Daily: remind one-time USDT users to renew manually within three days."""
    if _prelaunch_hold():
        # GK-443: a renewal nudge is member-facing and it sells. `notified_expiring`
        # is only set on a delivered reminder, so nobody's reminder is *lost* by
        # skipping — it is sent on the first run after the hold lifts, if they
        # are still inside the three-day window.
        logger.info("remind_expiring_job: skipped, prelaunch hold is on (GK-443)")
        return
    async with async_session() as session:
        rows = await expiring_manual_usdt(session, within_days=3)
        delivered = 0
        for sub in rows:
            if not getattr(sub, "user", None):
                await session.refresh(sub, ["user"])
            sent = await notify_usdt_expiring(
                sub.user.tg_id,
                sub.expires_at,
                reply_markup=usdt_renewal_keyboard(),
            )
            if not sent:
                continue
            sub.notified_expiring = True
            delivered += 1
            await session.commit()
    logger.info(
        "remind_expiring_job: %d eligible, %d delivered",
        len(rows),
        delivered,
    )


async def vimeo_sync_job() -> None:
    """GK-091/GK-092: refresh archive videos + showcase grouping from Vimeo daily.

    Runs the per-video metadata sync and the showcase→module sync together.
    Idempotent. A missing token or Vimeo outage is logged and leaves existing rows
    untouched (last-known data keeps rendering in the portal).
    """
    async with async_session() as session:
        result = await sync_archive(session)
        await session.commit()
    v, s = result.videos, result.showcases
    logger.info(
        "vimeo_sync_job: videos(ok=%s skipped=%s created=%d updated=%d hidden=%d) "
        "showcases(ok=%s skipped=%s modules=+%d/~%d memberships=+%d/-%d)",
        v.ok,
        v.skipped,
        v.created,
        v.updated,
        v.hidden,
        s.ok,
        s.skipped,
        s.modules_created,
        s.modules_updated,
        s.memberships_added,
        s.memberships_removed,
    )


async def reconciliation_job() -> None:
    """GK-070: daily provider/local discrepancy scan with Telegram ops summary."""
    async with async_session() as session:
        run = await run_reconciliation(session, triggered_by="scheduler")
        await session.commit()
    await send_ops_alert(
        reconciliation_summary_text(run),
        key=f"reconciliation_daily_{run.id}",
        rate_limit_seconds=3600,
        severity="warn" if run.open_items_count else "info",
    )
    logger.info(
        "reconciliation_job: run_id=%s items=%d open=%d",
        run.id,
        run.items_count,
        run.open_items_count,
    )


async def manual_cancellation_queue_job() -> None:
    """GK-433: daily reminder while any cancellation still needs a human.

    The per-request alert fires once, when the provider refuses. This is the
    part that stops an item from being forgotten afterwards: for as long as
    somebody's card is still live at the provider against their wishes, it says
    so once a day, with the days remaining before the next charge.

    Silent when the queue is empty — a daily "nothing to do" trains people to
    ignore the channel, which is how the original failure happened.
    """
    async with async_session() as session:
        rows = await open_manual_cancellations(session)
    if not rows:
        logger.info("manual_cancellation_queue_job: queue empty")
        return
    await send_ops_alert(
        manual_cancellation_digest_text(rows),
        key="manual_cancellation_queue_daily",
        # Under a day, so a bot restart cannot skip the reminder entirely, but
        # long enough that a restart loop cannot spam it either.
        rate_limit_seconds=20 * 3600,
        severity="error",
    )
    logger.warning("manual_cancellation_queue_job: %d open item(s)", len(rows))


async def backup_verification_job() -> None:
    """GK-437: notice when the restore canary stops proving anything.

    The canary writes its own failures into `backup_verifications`, so this job
    is not there to detect a failed restore — it is there to detect the case
    the canary cannot report on: itself being dead. A container that never
    starts, or dies after its first run, produces exactly the same database
    state as a system where nothing has gone wrong yet, and the difference is
    only visible by looking at the clock from somewhere else.

    Silent while the newest verification is a fresh pass. Anything else — never
    run, gone stale, or an outright failed restore — alerts once a day per
    state, with the state in the rate-limit key so a stale→failed transition
    is delivered rather than swallowed.
    """
    settings = get_settings()
    async with async_session() as session:
        row = await latest_verification(session)

    health = assess_backup_health(
        row, max_age_hours=settings.backup_verification_max_age_hours
    )
    if health.ok:
        logger.info(
            "backup_verification_job: ok, verified %.1fh ago (%s)",
            health.age_hours or 0.0,
            health.dump_file,
        )
        return

    await send_ops_alert(
        backup_alert_text(health, max_age_hours=settings.backup_verification_max_age_hours),
        key=next_alert_key(health),
        # Under a day so a bot restart cannot skip a whole cycle, long enough
        # that a restart loop cannot turn this into 48 messages (see GK-432).
        rate_limit_seconds=20 * 3600,
        severity="error",
    )
    logger.error(
        "backup_verification_job: state=%s age_hours=%s dump=%s",
        health.state,
        None if health.age_hours is None else round(health.age_hours, 1),
        health.dump_file,
    )


async def heartbeat_job() -> None:
    """GK-040: write a fresh heartbeat to Redis every 60s."""
    await record_bot_heartbeat()


async def scheduler_health_job(scheduler) -> None:
    """GK-040: alert Telegram if any scheduler job's next run is far in the past.

    APScheduler keeps `next_run_time` on each job; if a job stalled (DB stuck,
    Telegram outage) `next_run_time` lags behind wall clock. We compare with a
    generous tolerance — heartbeat (60s job) has its own freshness via Redis.
    """
    now = _dt.datetime.now(_dt.UTC)
    stalled: list[str] = []
    for job in scheduler.get_jobs():
        # `next_run_time` can legitimately be None mid-execution.
        nrt = getattr(job, "next_run_time", None)
        if nrt is None:
            continue
        # 2× the TTL of our tightest job (heartbeat) is the alert threshold.
        if (now - nrt).total_seconds() > HEARTBEAT_TTL_SECONDS * 2:
            stalled.append(job.id)
    if stalled:
        msg = "Scheduler stall detected — jobs lagging: " + ", ".join(stalled)
        logger.error(msg)
        await send_ops_alert(
            msg, key="scheduler_stall", rate_limit_seconds=900, severity="error"
        )
