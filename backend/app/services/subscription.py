from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Plan, Subscription, User, utcnow

settings = get_settings()

PROVIDER_RECURRING_SOURCES = {"stripe", "lava"}
ACCESS_HOLDING_STATUSES = {"active", "cancelled"}
PROVIDER_ACCESS_RESTORED_STATUSES = {"active", "trialing"}
DEFAULT_GRACE_DAYS = 3
DEFAULT_EXPIRY_REMINDER_DAYS = 3
MANUAL_USDT_SOURCE = "usdt"
#: GK-432: how many failed Telegram revoke attempts before the bot stops trying.
#: The job runs hourly, so this is a day of transient failures — long enough to
#: ride out an outage, short enough that nobody discovers attempt 647.
MAX_ACCESS_REVOKE_ATTEMPTS = 24


def is_comp_access(sub: Subscription) -> bool:
    """GK-483: this row is the client's own team, and it does not run out.

    The single place the comp rule is expressed. Everything that asks "does this
    member still have access" goes through `has_subscription_access`, so putting
    the rule there covers the Telegram channel, the hourly expiry job, the member
    portal and the 15.09 cutover tool at once, with no fourth copy to keep true.

    Chosen over the alternative the task offered — writing a far-future
    `expires_at` — because a date is a deadline that arrives. A comp row dated
    2099 is indistinguishable from a paid one in every query, survives no audit,
    and hands whoever is on duty that day an incident nobody wrote down. A rule
    is testable now (`test_comp_subscriptions.py`) and provable later with the
    same query GK-484 uses as its launch gate.
    """
    return bool(getattr(sub, "is_comp", False))


async def get_active_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    now = utcnow()
    q = (
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(ACCESS_HOLDING_STATUSES),
            Subscription.access_revoked_at.is_(None),
        )
        .order_by(Subscription.expires_at.desc())
    )
    res = await session.execute(q)
    for sub in res.scalars().all():
        if has_subscription_access(sub, now):
            return sub
    return None


async def has_portal_access(session: AsyncSession, user_id: int) -> bool:
    """True iff the user may use the Vimeo member portal right now (GK-091).

    Deliberately delegates to `get_active_subscription` so the portal shares the
    EXACT same access predicate as Telegram channel access: provider lifecycle,
    grace window, cancel-at-period-end, and `access_revoked_at` are all honored
    identically. A user kicked from the channel cannot reach the archive, and a
    user in provider grace keeps both.
    """
    return await get_active_subscription(session, user_id) is not None


def access_start_floor(now: datetime | None = None) -> datetime | None:
    """GK-439: the fixed date a **first** paid period may not start before.

    Returns the configured floor only while it is still in the future. A floor
    in the past is indistinguishable from no floor at all, which is what makes
    clearing the setting after launch a tidy-up rather than a deadline.

    This is the single decision point for the whole mechanism: the access clock
    below and the Stripe checkout shape (`stripe_provider._floored_first_period`)
    both read it, so the provider's next-charge date and our `expires_at` are
    derived from one answer instead of two that can disagree.
    """
    floor = settings.access_start_floor_at
    if floor is None:
        return None
    floor = _coerce_utc(floor)
    return floor if floor > _coerce_utc(now or utcnow()) else None


async def create_or_extend_subscription(
    session: AsyncSession,
    user: User,
    plan: Plan,
    source: str,
    extra_days: int = 0,
    invite_link: str | None = None,
    provider: str | None = None,
    provider_subscription_id: str | None = None,
    provider_status: str | None = "active",
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
    cancel_at_period_end: bool | None = None,
) -> Subscription:
    now = utcnow()
    current = await get_active_subscription(session, user.id)
    total_days = plan.duration_days + extra_days
    base_start = current.expires_at if current and current.expires_at > now else now
    period_start = current_period_start or base_start
    period_end = current_period_end or (base_start + timedelta(days=total_days))

    if current is None:
        # GK-439: a first paid period may not start before the launch floor, so
        # `base_start` is effectively max(now, floor). Renewals never reach this
        # branch — they chain from `expires_at` below and are untouched.
        #
        # The provider's own period dates are *overridden* here rather than
        # max()-ed with the floor, and that is deliberate: under the Stripe
        # prepaid-plus-trial shape the first invoice line covers the purchase,
        # not the access window, so its start and end are both the checkout
        # instant. Honouring it would end a member's access on the day it began.
        floor = access_start_floor(now)
        if floor is not None:
            period_start = floor
            period_end = floor + timedelta(days=total_days)

    if current:
        previous_expires_at = _coerce_utc(current.expires_at)
        next_expires_at = max(current.expires_at, period_end)
        current.expires_at = next_expires_at
        current.plan_id = plan.id
        current.source = source
        if source == MANUAL_USDT_SOURCE and _coerce_utc(next_expires_at) > previous_expires_at:
            current.notified_expiring = False
        if invite_link:
            current.invite_link = invite_link
        _apply_provider_lifecycle(
            current,
            provider=provider,
            provider_subscription_id=provider_subscription_id,
            provider_status=provider_status,
            current_period_start=period_start,
            current_period_end=current.expires_at,
            cancel_at_period_end=cancel_at_period_end,
        )
        await release_superseded_provider_link(
            session,
            provider_subscription_id=provider_subscription_id,
            keep_subscription_id=current.id,
        )
        return current

    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        status="active",
        source=source,
        started_at=period_start,
        expires_at=period_end,
        invite_link=invite_link,
        provider=provider,
        provider_subscription_id=provider_subscription_id,
        provider_status=provider_status,
        current_period_start=period_start,
        current_period_end=period_end,
        cancel_at_period_end=bool(cancel_at_period_end),
    )
    session.add(sub)
    await session.flush()
    await release_superseded_provider_link(
        session,
        provider_subscription_id=provider_subscription_id,
        keep_subscription_id=sub.id,
    )
    return sub


async def release_superseded_provider_link(
    session: AsyncSession,
    *,
    provider_subscription_id: str | None,
    keep_subscription_id: int | None,
) -> list[int]:
    """GK-426: exactly one local row may claim a provider subscription id.

    A renewal that lands *after* access lapsed does not extend the old row — it
    creates a new one, while the old row keeps the same
    ``provider_subscription_id`` and the ``provider_status`` it was frozen at.
    Reconciliation then compares that dead row against live provider state and
    reports a critical for it every night, forever (~1.5 per stale row per run).

    The row we just wrote or extended is the live one; every other claimant is
    history and releases the link. Nothing is lost: the provider ids stay on the
    ``payments`` rows, which is where the money's audit trail lives, and the
    period dates stay on the subscription, so access arithmetic is untouched.

    Returns the ids of the rows that were released.
    """
    if not provider_subscription_id:
        return []

    rows = (
        await session.execute(
            select(Subscription).where(
                Subscription.provider_subscription_id == provider_subscription_id
            )
        )
    ).scalars().all()

    released: list[int] = []
    for row in rows:
        if keep_subscription_id is not None and row.id == keep_subscription_id:
            continue
        row.provider_subscription_id = None
        row.provider_status = None
        released.append(row.id)
    return released


def _apply_provider_lifecycle(
    sub: Subscription,
    *,
    provider: str | None,
    provider_subscription_id: str | None,
    provider_status: str | None,
    current_period_start: datetime | None,
    current_period_end: datetime | None,
    cancel_at_period_end: bool | None,
) -> None:
    if provider is not None:
        sub.provider = provider
    if provider_subscription_id:
        sub.provider_subscription_id = provider_subscription_id
    if provider_status is not None:
        sub.provider_status = provider_status
        if provider_status in PROVIDER_ACCESS_RESTORED_STATUSES:
            sub.grace_started_at = None
            sub.grace_ends_at = None
            sub.access_revoke_retry_after_at = None
            sub.access_revoke_error = None
    if current_period_start is not None:
        sub.current_period_start = current_period_start
    if current_period_end is not None:
        sub.current_period_end = current_period_end
    if cancel_at_period_end is not None:
        sub.cancel_at_period_end = cancel_at_period_end


async def expire_subscriptions(session: AsyncSession) -> list[Subscription]:
    """Return subscriptions whose effective access has ended and needs revoke.

    The actual Telegram side effect is performed by the bot scheduler. We only
    flip the local status after that attempt is recorded, so a restart can retry
    failures and will not blindly re-process successful revokes.
    """
    now = utcnow()
    q = (
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            Subscription.status.in_(ACCESS_HOLDING_STATUSES),
            Subscription.access_revoked_at.is_(None),
            # GK-432: a removal the bot has given up on is not due again.
            Subscription.access_revoke_abandoned_at.is_(None),
            # GK-483: a comp row never falls due. `should_revoke_access` already
            # says so below; excluding it here as well means the team is not even
            # loaded by the hourly job, so the launch-gate query in GK-484 and
            # the job's own predicate return the same set.
            Subscription.is_comp.is_(False),
            or_(
                Subscription.access_revoke_retry_after_at.is_(None),
                Subscription.access_revoke_retry_after_at <= now,
            ),
        )
        .order_by(Subscription.expires_at.asc())
    )
    res = await session.execute(q)
    return [sub for sub in res.scalars().all() if should_revoke_access(sub, now)]


async def subscriptions_pending_ban_revoke(
    session: AsyncSession, *, now: datetime | None = None
) -> list[Subscription]:
    """Access-holding subscriptions of banned users not yet revoked from Telegram.

    GK-400: the admin ban action attempts the Telegram kick + invite revoke
    immediately, but a Telegram failure or process restart must still converge.
    The hourly ``kick_expired_job`` drains this set as the durable retry safety
    net. Unlike expiry, a banned member's access window may still be in the
    future, so this does not depend on ``should_revoke_access``.
    """
    now = _coerce_utc(now or utcnow())
    q = (
        select(Subscription)
        .join(User, User.id == Subscription.user_id)
        .options(selectinload(Subscription.user))
        .where(
            User.is_banned.is_(True),
            Subscription.status.in_(ACCESS_HOLDING_STATUSES),
            Subscription.access_revoked_at.is_(None),
            Subscription.access_revoke_abandoned_at.is_(None),
            or_(
                Subscription.access_revoke_retry_after_at.is_(None),
                Subscription.access_revoke_retry_after_at <= now,
            ),
        )
        .order_by(Subscription.id.asc())
    )
    res = await session.execute(q)
    return [sub for sub in res.scalars().all() if _ban_revoke_retry_ready(sub, now)]


def _ban_revoke_retry_ready(sub: Subscription, now: datetime) -> bool:
    retry_after_at = _coerce_optional_utc(getattr(sub, "access_revoke_retry_after_at", None))
    return retry_after_at is None or retry_after_at <= now


async def end_access_for_refund(
    session: AsyncSession,
    user_id: int,
    *,
    reason: str = "refund",
    now: datetime | None = None,
) -> list[Subscription]:
    """End a user's access immediately because their payment was fully refunded (GK-200).

    Collapses the access window to ``now`` and clears any grace so both the
    Telegram-channel predicate and the portal (`has_subscription_access` /
    `has_portal_access`) go False at once. Returns the still-joined subscriptions
    so the caller can issue the Telegram kick now; the row is left with
    ``access_revoked_at IS NULL`` so the hourly ``kick_expired_job`` is the
    idempotent safety net if the immediate kick fails or the process restarts.

    DB-only and idempotent: subscriptions already revoked or already in a
    non-access-holding status are skipped. A full refund does **not** cancel the
    provider-side subscription (a live API call) — the admin should cancel it in
    the provider dashboard to stop future renewals; reconciliation surfaces a
    remote-active-without-local-access discrepancy if they forget.
    """
    now = _coerce_utc(now or utcnow())
    rows = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(ACCESS_HOLDING_STATUSES),
            Subscription.access_revoked_at.is_(None),
        )
    )
    ended: list[Subscription] = []
    for sub in rows.scalars().all():
        sub.current_period_end = now
        sub.expires_at = now
        sub.grace_started_at = None
        sub.grace_ends_at = None
        sub.cancel_at_period_end = False
        sub.access_revoke_retry_after_at = None
        sub.provider_status = "refunded"
        if sub.status != "cancelled":
            sub.status = "cancelled"
        sub.access_revoke_error = f"refund: {reason}"[:1000]
        ended.append(sub)
    return ended


async def expiring_manual_usdt(
    session: AsyncSession,
    within_days: int = DEFAULT_EXPIRY_REMINDER_DAYS,
    *,
    now: datetime | None = None,
) -> list[Subscription]:
    """Return active one-time USDT subscriptions due a manual-renewal reminder."""
    now = _coerce_utc(now or utcnow())
    deadline = now + timedelta(days=within_days)
    q = (
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            Subscription.status == "active",
            Subscription.source == MANUAL_USDT_SOURCE,
            or_(
                Subscription.provider.is_(None),
                Subscription.provider == MANUAL_USDT_SOURCE,
            ),
            Subscription.provider_subscription_id.is_(None),
            Subscription.access_revoked_at.is_(None),
            Subscription.expires_at > now,
            Subscription.expires_at <= deadline,
            Subscription.notified_expiring.is_(False),
        )
        .order_by(Subscription.expires_at.asc())
    )
    res = await session.execute(q)
    return [
        sub
        for sub in res.scalars().all()
        if is_manual_usdt_expiry_reminder_due(sub, now=now, within_days=within_days)
    ]


def is_manual_usdt_expiry_reminder_due(
    sub: Subscription,
    *,
    now: datetime | None = None,
    within_days: int = DEFAULT_EXPIRY_REMINDER_DAYS,
) -> bool:
    """Pure eligibility predicate shared by the query and focused tests."""
    now = _coerce_utc(now or utcnow())
    expires_at = _coerce_optional_utc(getattr(sub, "expires_at", None))
    if expires_at is None or not (now < expires_at <= now + timedelta(days=within_days)):
        return False
    return bool(
        getattr(sub, "status", None) == "active"
        # GK-483: never nudge a comp member to renew something nobody bought.
        # `source` already excludes them today; this makes it a property of the
        # flag rather than a coincidence of how the row happened to be created.
        and not is_comp_access(sub)
        and getattr(sub, "source", None) == MANUAL_USDT_SOURCE
        and getattr(sub, "provider", None) in {None, MANUAL_USDT_SOURCE}
        and getattr(sub, "provider_subscription_id", None) is None
        and getattr(sub, "access_revoked_at", None) is None
        and not getattr(sub, "notified_expiring", False)
    )


def has_subscription_access(sub: Subscription, now: datetime | None = None) -> bool:
    if getattr(sub, "access_revoked_at", None) is not None:
        return False
    if getattr(sub, "status", None) not in ACCESS_HOLDING_STATUSES:
        return False
    # Deliberately after the two checks above, not before them: a banned team
    # member (`access_revoked_at`) is still removed, and a row somebody moved to
    # `expired` is still expired. The flag ends the *clock*, not the other rules.
    if is_comp_access(sub):
        return True
    access_ends_at = subscription_access_ends_at(sub)
    if access_ends_at is None:
        return False
    return access_ends_at > _coerce_utc(now or utcnow())


def should_revoke_access(sub: Subscription, now: datetime | None = None) -> bool:
    now = _coerce_utc(now or utcnow())
    if has_subscription_access(sub, now):
        return False
    if getattr(sub, "access_revoked_at", None) is not None:
        return False
    if getattr(sub, "access_revoke_abandoned_at", None) is not None:
        return False
    if getattr(sub, "status", None) not in ACCESS_HOLDING_STATUSES:
        return False
    retry_after_at = _coerce_optional_utc(getattr(sub, "access_revoke_retry_after_at", None))
    return retry_after_at is None or retry_after_at <= now


def subscription_access_ends_at(sub: Subscription) -> datetime | None:
    paid_until = (
        _coerce_optional_utc(getattr(sub, "current_period_end", None))
        if _has_provider_lifecycle(sub)
        else None
    )
    if paid_until is None:
        paid_until = _coerce_optional_utc(getattr(sub, "expires_at", None))

    grace_until = _coerce_optional_utc(getattr(sub, "grace_ends_at", None))
    candidates = [dt for dt in (paid_until, grace_until) if dt is not None]
    return max(candidates) if candidates else None


def start_provider_grace(
    sub: Subscription,
    *,
    now: datetime | None = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
    provider_status: str = "past_due",
) -> None:
    now = _coerce_utc(now or utcnow())
    paid_until = _coerce_optional_utc(getattr(sub, "current_period_end", None))
    if paid_until is None:
        paid_until = _coerce_optional_utc(getattr(sub, "expires_at", None))

    grace_started_at = max(dt for dt in (paid_until, now) if dt is not None)
    grace_ends_at = grace_started_at + timedelta(days=grace_days)
    current_grace_ends_at = _coerce_optional_utc(getattr(sub, "grace_ends_at", None))

    if getattr(sub, "grace_started_at", None) is None:
        sub.grace_started_at = grace_started_at
    if current_grace_ends_at is None or current_grace_ends_at < grace_ends_at:
        sub.grace_ends_at = grace_ends_at
    if provider_status:
        sub.provider_status = provider_status


def record_access_revoke_attempt(
    sub: Subscription,
    *,
    success: bool,
    retry_after_seconds: int | None = None,
    error: str | None = None,
    now: datetime | None = None,
    mark_status_expired: bool = True,
    permanent: bool = False,
) -> bool:
    """Record the outcome of a Telegram access-revoke attempt on the subscription.

    ``mark_status_expired`` flips the subscription to ``expired`` on success and is
    correct for expiry/refund revocation. A **ban** (GK-400) passes ``False`` so the
    paid subscription state is preserved — the ban revokes access via
    ``access_revoked_at`` (which the portal and channel predicate already honor),
    and an admin unban can cleanly restore a still-entitled subscription.

    GK-432: returns ``True`` when *this* attempt was the one that gave up, so the
    caller can alert exactly once. A removal is abandoned when Telegram's refusal
    is permanent (``permanent=True`` — a chat owner, a bot without rights, a chat
    that is gone) or when the attempt bound is reached. Before this, a row simply
    retried every hour forever and looked, to anyone reading it, like an attempt
    still in progress.
    """
    now = _coerce_utc(now or utcnow())
    sub.access_revoke_attempted_at = now
    sub.access_revoke_attempts = (getattr(sub, "access_revoke_attempts", 0) or 0) + 1

    if success:
        sub.access_revoked_at = now
        sub.access_revoke_retry_after_at = None
        sub.access_revoke_error = None
        sub.access_revoke_abandoned_at = None
        if mark_status_expired and getattr(sub, "status", None) != "cancelled":
            sub.status = "expired"
        return False

    sub.access_revoke_error = _truncate_error(error)
    if retry_after_seconds is not None and retry_after_seconds > 0:
        sub.access_revoke_retry_after_at = now + timedelta(seconds=retry_after_seconds)
    else:
        sub.access_revoke_retry_after_at = None

    if getattr(sub, "access_revoke_abandoned_at", None) is not None:
        return False
    exhausted = sub.access_revoke_attempts >= MAX_ACCESS_REVOKE_ATTEMPTS
    if permanent or exhausted:
        sub.access_revoke_abandoned_at = now
        sub.access_revoke_retry_after_at = None
        return True
    return False


def _has_provider_lifecycle(sub: Subscription) -> bool:
    provider = getattr(sub, "provider", None) or getattr(sub, "source", None)
    return bool(
        provider in PROVIDER_RECURRING_SOURCES
        and (
            getattr(sub, "provider_subscription_id", None)
            or getattr(sub, "provider_status", None)
            or getattr(sub, "current_period_end", None)
        )
    )


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _coerce_utc(value)


def _truncate_error(error: str | None) -> str | None:
    if not error:
        return None
    return error[:1000]
