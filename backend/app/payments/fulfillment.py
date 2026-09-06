"""Once a Payment row reaches status='succeeded', call fulfill_payment.

`Payment.approved_at` is the local fulfillment marker: once set, re-fulfilling
the same payment is a no-op. Provider invoice/event fields are also checked so
duplicate webhook rows cannot grant duplicate days. Used by Stripe/Lava webhooks
and admin "approve manual payment".
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, Plan, Subscription, User, utcnow
from app.observability import send_ops_alert
from app.services.channel_access import create_invite_links
from app.services.gifts import ensure_paid_gift_activation
from app.services.referral import consume_referral_discount_reservation
from app.services.referral_ledger import record_referral_commission_intent
from app.services.subscription import create_or_extend_subscription
from app.services.webhooks import dispatch

logger = logging.getLogger(__name__)


async def fulfill_payment(session: AsyncSession, bot: Bot | None, payment: Payment) -> Subscription | None:
    if payment.status != "succeeded":
        logger.warning("fulfill called on non-succeeded payment %s", payment.id)
        return None

    if payment.approved_at is not None:
        logger.info("payment %s already fulfilled; skipping side effects", payment.id)
        return None

    duplicate = await _find_fulfilled_provider_duplicate(session, payment)
    if duplicate is not None:
        logger.info(
            "payment %s duplicates fulfilled provider event/invoice on payment %s",
            payment.id,
            duplicate.id,
        )
        payment.approved_at = duplicate.approved_at or utcnow()
        return None

    plan = (await session.execute(select(Plan).where(Plan.id == payment.plan_id))).scalar_one_or_none()
    if plan is None:
        logger.error("plan %s missing for payment %s", payment.plan_id, payment.id)
        return None

    if payment.is_gift and payment.gift_recipient_id is None:
        return await _fulfill_unclaimed_gift(session, payment, plan)

    recipient_id = _recipient_id(payment)
    if recipient_id is None:
        logger.error("recipient missing for gift payment %s", payment.id)
        return None

    recipient = (await session.execute(select(User).where(User.id == recipient_id))).scalar_one_or_none()
    if recipient is None:
        logger.error("recipient %s missing for payment %s", recipient_id, payment.id)
        return None

    invite = None
    invite_error_summary = None
    if _should_issue_invite(payment) and bot is not None:
        invite_result = await create_invite_links(bot, name=f"membership_saas pay#{payment.id}")
        invite = invite_result.storage_text
        if not invite_result.all_success:
            invite_error_summary = (
                invite_result.error_summary or "no Telegram access links created"
            )
            _append_payment_note(
                payment,
                "[telegram access grant issue] " + invite_error_summary,
            )
    if _should_issue_invite(payment) and invite is None:
        logger.error(
            "invite link creation failed for payment %s; fulfillment continues, "
            "admin must regenerate manually",
            payment.id,
        )
        await _send_invite_failure_alert(
            payment,
            error_summary=invite_error_summary or "bot unavailable; no Telegram access links created",
            total_failure=True,
        )
    elif _should_issue_invite(payment) and invite_error_summary is not None:
        logger.error(
            "partial Telegram access grant for payment %s; note=%s",
            payment.id,
            payment.note,
        )
        await _send_invite_failure_alert(
            payment,
            error_summary=invite_error_summary or "some Telegram access links were not created",
            total_failure=False,
        )

    sub = await create_or_extend_subscription(
        session,
        user=recipient,
        plan=plan,
        source="gift" if payment.is_gift else payment.provider,
        invite_link=invite,
        provider=None if payment.is_gift else payment.provider,
        provider_subscription_id=_provider_subscription_id(payment),
        provider_status=None if payment.is_gift else "active",
        # Gift access is a fixed duration for the recipient. Provider billing
        # dates describe the buyer's charge and can be stale by redemption time;
        # passing them through would make an active recipient lose gift days.
        current_period_start=None if payment.is_gift else payment.billing_period_start,
        current_period_end=None if payment.is_gift else payment.billing_period_end,
        cancel_at_period_end=False,
    )

    if not payment.is_gift:
        await record_referral_commission_intent(
            session,
            recipient,
            payment,
            coverage_start=(
                payment.billing_period_start
                or getattr(sub, "current_period_start", None)
                or getattr(sub, "started_at", None)
            ),
            coverage_end=(
                payment.billing_period_end
                or getattr(sub, "current_period_end", None)
                or sub.expires_at
            ),
        )
        # GK-402: settle the one-time referral discount exactly once. Only this
        # payment's own ACTIVE reservation is consumed; a stale/reclaimed slot is
        # a no-op, so two late-completing checkouts never spend the benefit twice.
        await consume_referral_discount_reservation(session, payment)

    payment.approved_at = utcnow()

    await dispatch(
        session,
        "payment.succeeded",
        {
            "payment_id": payment.id,
            "user_tg_id": recipient.tg_id,
            "amount": float(payment.amount),
            "currency": payment.currency,
            "plan": plan.code,
            "is_gift": payment.is_gift,
            "is_renewal": payment.is_renewal,
            "provider": payment.provider,
        },
    )
    await dispatch(
        session,
        "subscription.renewed" if payment.is_renewal else "subscription.activated",
        {
            "subscription_id": sub.id,
            "user_tg_id": recipient.tg_id,
            "plan": plan.code,
            "expires_at": sub.expires_at.isoformat(),
            "provider": sub.provider,
            "provider_status": sub.provider_status,
        },
    )

    return sub


async def _fulfill_unclaimed_gift(
    session: AsyncSession,
    payment: Payment,
    plan: Plan,
) -> None:
    buyer = (await session.execute(select(User).where(User.id == payment.user_id))).scalar_one_or_none()
    if buyer is None:
        logger.error("gift buyer %s missing for payment %s", payment.user_id, payment.id)
        return None

    gift = await ensure_paid_gift_activation(session, payment)
    if gift is None:
        logger.error("gift activation record was not created for payment %s", payment.id)
        return None

    payment.approved_at = utcnow()
    await dispatch(
        session,
        "payment.succeeded",
        {
            "payment_id": payment.id,
            "user_tg_id": buyer.tg_id,
            "amount": float(payment.amount),
            "currency": payment.currency,
            "plan": plan.code,
            "is_gift": True,
            "is_renewal": False,
            "provider": payment.provider,
            "gift_id": gift.id,
        },
    )
    return None


def _recipient_id(payment: Payment) -> int | None:
    return payment.gift_recipient_id if payment.is_gift else payment.user_id


def _should_issue_invite(payment: Payment) -> bool:
    return not payment.is_renewal


def _provider_subscription_id(payment: Payment) -> str | None:
    if payment.is_gift:
        return None
    if payment.provider == "stripe":
        return payment.external_id
    if payment.provider == "lava":
        return payment.lava_subscription_id
    return None


def _append_payment_note(payment: Payment, note: str) -> None:
    current = (getattr(payment, "note", None) or "").rstrip()
    payment.note = f"{current}\n{note}".strip() if current else note


async def _send_invite_failure_alert(
    payment: Payment,
    *,
    error_summary: str,
    total_failure: bool,
) -> None:
    """Page ops without allowing the alert path to break fulfillment.

    Plain text, no markup and no escaping here: GK-451 escapes the whole body
    inside `send_ops_alert`, so a `<code>` wrapper would arrive as the literal
    `&lt;code&gt;` and an `html.escape` at this call site would escape a second
    time — `error_summary` comes from Telegram's API and can carry an `&`.
    `test_no_ops_alert_ships_html_markup_in_its_body` enforces this.
    """
    payment_id = str(payment.id)
    provider = str(payment.provider)
    error = error_summary[:1000]
    if total_failure:
        headline = "Не удалось создать ни одной ссылки Telegram после оплаты."
        action = "Подписка будет активирована без ссылки; требуется вручную выдать доступ."
        key_kind = "total"
        severity = "error"
    else:
        headline = "Доступ в Telegram выдан не полностью после оплаты."
        action = "Созданные ссылки сохранены; требуется вручную выдать недостающий доступ."
        key_kind = "partial"
        severity = "warn"

    try:
        await send_ops_alert(
            f"{headline}\n"
            f"payment_id={payment_id} provider={provider}\n"
            f"Ошибка: {error}\n"
            f"{action}",
            key=f"payment_invite_{key_kind}:{payment.id}",
            rate_limit_seconds=3600,
            severity=severity,
        )
    except Exception:  # noqa: BLE001 — alerting must never block paid access
        logger.warning(
            "failed to dispatch %s invite-link ops alert for payment %s",
            key_kind,
            payment.id,
            exc_info=True,
        )


async def _find_fulfilled_provider_duplicate(
    session: AsyncSession,
    payment: Payment,
) -> Payment | None:
    filters = []
    if payment.provider_event_id:
        filters.append(
            (Payment.provider == payment.provider)
            & (Payment.provider_event_id == payment.provider_event_id)
        )
    if payment.stripe_invoice_id:
        filters.append(Payment.stripe_invoice_id == payment.stripe_invoice_id)
    if payment.lava_invoice_id:
        filters.append(
            (Payment.provider == payment.provider)
            & (Payment.lava_invoice_id == payment.lava_invoice_id)
        )
    if payment.tx_hash and payment.tx_network:
        filters.append(
            (Payment.tx_network == payment.tx_network)
            & (Payment.tx_hash == payment.tx_hash)
        )

    if not filters:
        return None

    q = (
        select(Payment)
        .where(
            Payment.id != payment.id,
            Payment.status == "succeeded",
            Payment.approved_at.is_not(None),
            or_(*filters),
        )
        .order_by(Payment.approved_at.desc())
        .limit(1)
    )
    return (await session.execute(q)).scalars().first()
