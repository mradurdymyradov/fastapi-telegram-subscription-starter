"""Incoming webhooks from payment providers."""
import hashlib
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB
from app.config import get_settings
from app.db.models import Payment, PaymentProviderEvent, Subscription, User
from app.observability import bind_log_context, send_ops_alert
from app.payments.fulfillment import fulfill_payment
from app.payments.lava_provider import LavaProvider
from app.payments.stripe_provider import StripeProvider, StripeWebhookVerificationError

# GK-459: these three are the API's entire member-facing voice — every Russian
# sentence a Stripe or Lava webhook can put in front of a member comes from this
# module and nowhere else in this file. They are GK-443-aware from the inside
# (`prelaunch_hold_applies_to`), so a webhook arriving while the hold is on no
# longer answers «продлите через /subscribe» in a bot that has no /subscribe.
#
# Fulfilment is deliberately *not* held: a member who paid gets the subscription,
# the invite and the notice regardless. GK-444 measured that path with the bot
# container stopped and it is the behaviour we want — the hold removes the sell,
# not the goods.
from app.services.billing_notifications import (
    notify_payment_failed,
    notify_payment_succeeded,
    notify_subscription_cancelled,
)
from app.services.referral_ledger import cancel_pending_commission_for_referee
from app.services.subscription import start_provider_grace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
settings = get_settings()

# GK-404: both webhook endpoints are public and authenticate the caller only
# AFTER the raw body is read (Stripe verifies a signature over it; Lava checks a
# header credential). An anonymous caller could otherwise stream an unbounded
# body and exhaust memory/workers before that check runs, so cap the body here,
# before it is buffered / parsed / verified. Caddy also caps /webhooks/* at 1MB
# in front of the app (defence in depth).
MAX_WEBHOOK_BODY_BYTES = 1_048_576  # 1 MiB


async def _read_body_within_limit(request: Request) -> bytes:
    """Return the request body, rejecting anything over ``MAX_WEBHOOK_BODY_BYTES``.

    Rejects a declared oversize ``Content-Length`` before reading a byte, and
    independently caps the streamed size so a missing or under-reported
    ``Content-Length`` (e.g. chunked transfer-encoding) cannot slip past. Raises
    ``HTTPException(413)`` before the caller parses or verifies anything.
    """
    max_bytes = MAX_WEBHOOK_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
        if declared_len > max_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
    return bytes(body)


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: DB,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    body = await _read_body_within_limit(request)
    try:
        event = StripeProvider.parse_webhook(body, stripe_signature)
    except StripeWebhookVerificationError as exc:
        raise HTTPException(400, str(exc)) from exc
    if event is None:
        return {"ok": True, "ignored": True}

    bind_log_context(
        provider="stripe",
        provider_event_id=_stripe_event_id(event),
        provider_event_type=event.status,
    )

    event_record = await _record_stripe_event_once(
        db,
        event,
        raw_hash=hashlib.sha256(body).hexdigest(),
    )
    if event_record is None:
        return {"ok": True, "already": True}

    if event.status == "checkout_completed":
        payment = await _payment_from_metadata_id(db, event.metadata)
        if payment is None:
            raise HTTPException(400, "Bad request")
        return await _handle_stripe_checkout_completed(db, event, event_record, payment)

    if event.status == "invoice_paid":
        return await _handle_stripe_invoice_paid(db, event, event_record)
    if event.status == "invoice_payment_failed":
        return await _handle_stripe_invoice_payment_failed(db, event, event_record)
    if event.status in {"subscription_updated", "subscription_deleted"}:
        return await _handle_stripe_subscription_event(db, event)
    return {"ok": True, "ignored": True}


async def _record_stripe_event_once(
    db: DB,
    event,
    *,
    raw_hash: str,
) -> PaymentProviderEvent | None:
    event_id = _stripe_event_id(event)
    if not event_id:
        raise HTTPException(400, "Bad request")

    record = PaymentProviderEvent(
        provider="stripe",
        event_id=event_id,
        event_type=event.metadata.get("stripe_event_type") or event.status,
        raw_hash=raw_hash,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return None
    return record


async def _handle_stripe_invoice_paid(
    db: DB,
    event,
    event_record: PaymentProviderEvent,
) -> dict:
    payment = await _find_stripe_payment_for_invoice(db, event.metadata)
    if payment is None:
        payment = await _create_stripe_payment_from_invoice(db, event)
    if payment is None:
        return {"ok": True, "ignored": True}

    event_record.payment_id = payment.id
    if payment.approved_at is not None:
        return {"ok": True, "already": True}

    _bind_stripe_invoice_fields(payment, event, status="succeeded")
    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    try:
        sub = await fulfill_payment(db, bot, payment)
    finally:
        if bot:
            await bot.session.close()
    await notify_payment_succeeded(db, payment, sub)
    return {"ok": True, "fulfilled": True}


async def _handle_stripe_checkout_completed(
    db: DB,
    event,
    event_record: PaymentProviderEvent,
    payment: Payment,
) -> dict:
    """Bind every Checkout, but fulfill only paid one-time gift Checkouts.

    Normal Stripe subscriptions remain invoice-centric: their
    ``checkout.session.completed`` event only binds provider IDs and
    ``invoice.paid`` grants access. Gifts use Checkout ``mode=payment`` so they
    have no invoice lifecycle; a signed, paid completion is their entitlement
    event and creates the one-time activation link.
    """
    event_record.payment_id = payment.id
    await _bind_stripe_checkout_completion(db, payment, event.metadata)
    if not bool(getattr(payment, "is_gift", False)):
        return {"ok": True, "bound": True}
    if payment.approved_at is not None:
        return {"ok": True, "already": True}
    if str(event.metadata.get("stripe_payment_status") or "").lower() != "paid":
        return {"ok": True, "bound": True, "payment_pending": True}

    payment.status = "succeeded"
    payment.amount = Decimal(str(event.amount))
    payment.currency = event.currency
    payment.provider_event_id = _stripe_event_id(event)
    payment_intent_id = event.metadata.get("stripe_payment_intent_id")
    if payment_intent_id:
        payment.stripe_payment_intent_id = payment_intent_id

    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    try:
        sub = await fulfill_payment(db, bot, payment)
    finally:
        if bot:
            await bot.session.close()
    await notify_payment_succeeded(db, payment, sub)
    return {"ok": True, "fulfilled": True}


async def _handle_stripe_invoice_payment_failed(
    db: DB,
    event,
    event_record: PaymentProviderEvent,
) -> dict:
    payment = await _find_stripe_payment_for_invoice(db, event.metadata)
    if payment is not None:
        event_record.payment_id = payment.id
        if payment.approved_at is None:
            _bind_stripe_invoice_fields(payment, event, status="failed")

    sub = await _find_stripe_subscription(
        db,
        event.metadata.get("stripe_subscription_id"),
        lock=True,
    )
    if sub is not None:
        start_provider_grace(sub, provider_status="past_due")
    await notify_payment_failed(db, payment=payment, subscription=sub, provider="stripe")
    return {"ok": True, "updated": True}


async def _handle_stripe_subscription_event(db: DB, event) -> dict:
    sub = await _find_stripe_subscription(
        db,
        event.metadata.get("stripe_subscription_id"),
        lock=True,
    )
    if sub is None:
        return {"ok": True, "ignored": True}

    provider_status = event.metadata.get("stripe_subscription_status") or None
    if provider_status:
        sub.provider_status = provider_status
    period_start = _metadata_dt(event.metadata, "stripe_subscription_current_period_start")
    period_end = _metadata_dt(event.metadata, "stripe_subscription_current_period_end")
    if period_start is not None:
        sub.current_period_start = period_start
    if period_end is not None:
        sub.current_period_end = period_end
    sub.cancel_at_period_end = _metadata_bool(
        event.metadata,
        "stripe_subscription_cancel_at_period_end",
    )
    if event.status == "subscription_deleted" or provider_status in {"canceled", "cancelled"}:
        sub.status = "cancelled"
        await cancel_pending_commission_for_referee(
            db,
            sub.user_id,
            reason="stripe.subscription_cancelled",
        )
        await notify_subscription_cancelled(db, sub, provider="stripe")
    elif provider_status in {"active", "trialing"} and sub.status == "cancelled":
        sub.status = "active"
    return {"ok": True, "updated": True}


async def _bind_stripe_checkout_completion(db: DB, payment: Payment, metadata: dict) -> None:
    """Persist Checkout-created Stripe IDs without provisioning access."""
    checkout_session_id = metadata.get("stripe_checkout_session_id")
    customer_id = metadata.get("stripe_customer_id")
    subscription_id = metadata.get("stripe_subscription_id")

    if checkout_session_id:
        payment.stripe_checkout_session_id = checkout_session_id
    if subscription_id:
        payment.external_id = subscription_id
    if customer_id:
        user = (
            await db.execute(select(User).where(User.id == payment.user_id).with_for_update())
        ).scalar_one_or_none()
        if user is not None:
            user.stripe_customer_id = customer_id


async def _payment_from_metadata_id(db: DB, metadata: dict) -> Payment | None:
    payment_id = _metadata_int(metadata, "payment_id")
    if payment_id is None:
        return None
    return (
        await db.execute(
            select(Payment)
            .where(Payment.id == payment_id, Payment.provider == "stripe")
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _find_stripe_payment_for_invoice(db: DB, metadata: dict) -> Payment | None:
    invoice_id = metadata.get("stripe_invoice_id")
    if invoice_id:
        payment = (
            await db.execute(
                select(Payment)
                .where(Payment.provider == "stripe", Payment.stripe_invoice_id == invoice_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if payment is not None:
            return payment

    if not _invoice_is_renewal(metadata):
        payment = await _payment_from_metadata_id(db, metadata)
        if payment is not None:
            return payment

    checkout_session_id = metadata.get("stripe_checkout_session_id")
    if checkout_session_id:
        payment = (
            await db.execute(
                select(Payment)
                .where(
                    Payment.provider == "stripe",
                    Payment.stripe_checkout_session_id == checkout_session_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if payment is not None:
            return payment

    subscription_id = metadata.get("stripe_subscription_id")
    if subscription_id:
        return (
            await db.execute(
                select(Payment)
                .where(
                    Payment.provider == "stripe",
                    Payment.external_id == subscription_id,
                    Payment.status == "pending",
                )
                .order_by(Payment.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
    return None


async def _create_stripe_payment_from_invoice(db: DB, event) -> Payment | None:
    metadata = event.metadata
    subscription_id = metadata.get("stripe_subscription_id")
    local_sub = await _find_stripe_subscription(db, subscription_id, lock=True)
    user_id = _metadata_int(metadata, "user_id")
    plan_id = _metadata_int(metadata, "plan_id")
    gift_recipient_id = _metadata_int(metadata, "gift_recipient_id")
    is_gift = _metadata_bool(metadata, "is_gift") or gift_recipient_id is not None

    if local_sub is not None:
        user_id = user_id or local_sub.user_id
        plan_id = plan_id or local_sub.plan_id

    if user_id is None or plan_id is None:
        return None

    payment = Payment(
        user_id=user_id,
        plan_id=plan_id,
        provider="stripe",
        amount=Decimal(str(event.amount)),
        currency=event.currency,
        status="pending",
        is_gift=is_gift,
        gift_recipient_id=gift_recipient_id,
        is_renewal=_invoice_is_renewal(metadata)
        or (local_sub is not None and _metadata_int(metadata, "payment_id") is None),
    )
    _bind_stripe_invoice_fields(payment, event, status="succeeded")
    db.add(payment)
    await db.flush()
    return payment


def _bind_stripe_invoice_fields(payment: Payment, event, *, status: str) -> None:
    metadata = event.metadata
    subscription_id = metadata.get("stripe_subscription_id")
    invoice_id = metadata.get("stripe_invoice_id")

    payment.status = status
    payment.provider = "stripe"
    payment.amount = Decimal(str(event.amount))
    payment.currency = event.currency
    payment.provider_event_id = _stripe_event_id(event)
    if invoice_id:
        payment.stripe_invoice_id = invoice_id
    # GK-200: keep the payment intent so a later refund can target the charge by
    # payment intent without re-fetching the invoice from Stripe.
    payment_intent_id = metadata.get("stripe_payment_intent_id")
    if payment_intent_id:
        payment.stripe_payment_intent_id = payment_intent_id
    if subscription_id:
        payment.external_id = subscription_id
    if _invoice_is_renewal(metadata):
        payment.is_renewal = True

    period_start = _metadata_dt(metadata, "stripe_invoice_period_start")
    period_end = _metadata_dt(metadata, "stripe_invoice_period_end")
    if period_start is not None:
        payment.billing_period_start = period_start
    if period_end is not None:
        payment.billing_period_end = period_end


async def _find_stripe_subscription(
    db: DB,
    subscription_id: str | None,
    *,
    lock: bool = False,
) -> Subscription | None:
    if not subscription_id:
        return None
    q = (
        select(Subscription)
        .where(
            Subscription.provider == "stripe",
            Subscription.provider_subscription_id == subscription_id,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    if lock:
        q = q.with_for_update()
    return (await db.execute(q)).scalar_one_or_none()


def _stripe_event_id(event) -> str:
    return str(event.metadata.get("stripe_event_id") or event.external_id or "")


def _metadata_int(metadata: dict, key: str) -> int | None:
    raw = metadata.get(key)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _metadata_dt(metadata: dict, key: str) -> datetime | None:
    raw = metadata.get(key)
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw.astimezone(UTC) if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    try:
        value = int(raw)
        if value > 10_000_000_000:
            value = value // 1000
        return datetime.fromtimestamp(value, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _metadata_bool(metadata: dict, key: str) -> bool:
    raw = str(metadata.get(key) or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _invoice_is_renewal(metadata: dict) -> bool:
    return metadata.get("stripe_invoice_billing_reason") in {
        "subscription_cycle",
        "subscription_threshold",
        "subscription_update",
    }


@router.post("/lava")
async def lava_webhook(
    request: Request,
    db: DB,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    raw = await _read_body_within_limit(request)
    if not LavaProvider.verify_webhook_auth(
        x_api_key=x_api_key,
        authorization=authorization,
    ):
        raise HTTPException(401, "Invalid Lava webhook authentication")

    event = LavaProvider.parse_webhook(raw)
    if event is None:
        return {"ok": True, "ignored": True}

    bind_log_context(
        provider="lava",
        provider_event_id=_lava_event_id(event),
        provider_event_type=event.status,
    )

    event_record = await _record_lava_event_once(
        db,
        event,
        raw_hash=hashlib.sha256(raw).hexdigest(),
    )
    if event_record is None:
        return {"ok": True, "already": True}

    if event.status == "invoice_paid":
        return await _handle_lava_invoice_paid(db, event, event_record)
    if event.status == "invoice_payment_failed":
        return await _handle_lava_invoice_payment_failed(db, event, event_record)
    if event.status == "subscription_deleted":
        return await _handle_lava_subscription_event(db, event)
    return {"ok": True, "ignored": True}


async def _record_lava_event_once(
    db: DB,
    event,
    *,
    raw_hash: str,
) -> PaymentProviderEvent | None:
    event_id = _lava_event_id(event)
    if not event_id:
        raise HTTPException(400, "Bad request")

    record = PaymentProviderEvent(
        provider="lava",
        event_id=event_id,
        event_type=event.metadata.get("lava_event_type") or event.status,
        raw_hash=raw_hash,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return None
    return record


async def _handle_lava_invoice_paid(
    db: DB,
    event,
    event_record: PaymentProviderEvent,
) -> dict:
    payment = await _find_lava_payment_for_invoice(db, event.metadata)
    if payment is None:
        payment = await _create_lava_payment_from_invoice(db, event)
    if payment is None:
        # GK-416: Lava took money and we cannot attach it to anyone. We still
        # answer 200 (retrying would not help — the correlation data is simply
        # absent), so this alert is the only trace the charge ever happened.
        await _alert_unactionable_lava_charge(
            event,
            headline="Оплата Lava не сопоставлена ни с одним платежом",
            detail=(
                "Списание прошло, доступ НЕ выдан. Найдите контракт в кабинете "
                "Lava, определите покупателя и проведите платёж вручную."
            ),
        )
        return {"ok": True, "ignored": True}

    event_record.payment_id = payment.id
    if payment.approved_at is not None:
        return {"ok": True, "already": True}

    # GK-412: page-flow payments (no invoice pre-created, so no
    # lava_invoice_id yet) are correlated by clientUtm payment_id, which a
    # buyer could carry onto a cheaper offer of the same Lava product. Only
    # fulfill when the paid total matches what the bot recorded for the plan;
    # otherwise leave the payment pending for admin review.
    if (
        not payment.lava_invoice_id
        and not _lava_invoice_is_renewal(event.metadata)
        and not _lava_amount_matches_payment(payment, event)
    ):
        logger.error(
            "lava paid event amount mismatch: payment_id=%s expected %s %s got %s %s",
            payment.id,
            payment.amount,
            payment.currency,
            event.amount,
            event.currency,
        )
        await _alert_unactionable_lava_charge(
            event,
            headline="Оплата Lava на сумму, отличную от ожидаемой",
            detail=(
                f"Платёж #{payment.id} оставлен в статусе pending и ждёт решения "
                f"администратора (ожидали {payment.amount} {payment.currency})."
            ),
        )
        return {"ok": True, "amount_mismatch": True}

    _bind_lava_invoice_fields(payment, event, status="succeeded")
    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    try:
        sub = await fulfill_payment(db, bot, payment)
    finally:
        if bot:
            await bot.session.close()
    await notify_payment_succeeded(db, payment, sub)
    return {"ok": True, "fulfilled": True}


async def _handle_lava_invoice_payment_failed(
    db: DB,
    event,
    event_record: PaymentProviderEvent,
) -> dict:
    payment = await _find_lava_payment_for_invoice(db, event.metadata)
    if payment is not None:
        event_record.payment_id = payment.id
        if payment.approved_at is None:
            _bind_lava_invoice_fields(payment, event, status="failed")

    sub = await _find_lava_subscription(
        db,
        event.metadata.get("lava_subscription_id"),
        lock=True,
    )
    if sub is not None:
        start_provider_grace(sub, provider_status="past_due")
    await notify_payment_failed(db, payment=payment, subscription=sub, provider="lava")
    return {"ok": True, "updated": True}


async def _handle_lava_subscription_event(db: DB, event) -> dict:
    sub = await _find_lava_subscription(
        db,
        event.metadata.get("lava_subscription_id"),
        lock=True,
    )
    if sub is None:
        return {"ok": True, "ignored": True}

    provider_status = event.metadata.get("lava_contract_status") or "cancelled"
    if provider_status:
        sub.provider_status = provider_status
    period_start = _metadata_dt(event.metadata, "lava_period_start")
    period_end = _metadata_dt(event.metadata, "lava_period_end")
    if period_start is not None:
        sub.current_period_start = period_start
    if period_end is not None:
        sub.current_period_end = period_end
    if event.status == "subscription_deleted" or provider_status in {"cancelled", "canceled"}:
        # Lava's subscription.cancelled webhook is an auto-renew cancellation,
        # not an immediate access revocation. The official payload supplies
        # willExpireAt; keep access active through that paid-through date and let
        # the normal expiry job revoke it afterwards.
        sub.cancel_at_period_end = True
        if period_end is not None:
            sub.expires_at = period_end
        access_end = period_end or sub.expires_at
        if access_end is not None and access_end <= datetime.now(UTC):
            sub.status = "cancelled"
            sub.cancel_at_period_end = False
        await cancel_pending_commission_for_referee(
            db,
            sub.user_id,
            reason="lava.subscription_cancelled",
        )
        await notify_subscription_cancelled(db, sub, provider="lava")
    else:
        sub.cancel_at_period_end = _metadata_bool(
            event.metadata,
            "lava_cancel_at_period_end",
        )
        if provider_status in {"active", "trialing"} and sub.status == "cancelled":
            sub.status = "active"
    return {"ok": True, "updated": True}


async def _find_lava_payment_for_invoice(db: DB, metadata: dict) -> Payment | None:
    invoice_id = metadata.get("lava_invoice_id")
    if invoice_id:
        payment = (
            await db.execute(
                select(Payment)
                .where(Payment.provider == "lava", Payment.lava_invoice_id == invoice_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if payment is not None:
            return payment

    if not _lava_invoice_is_renewal(metadata):
        payment_id = _metadata_int(metadata, "payment_id")
        if payment_id is not None:
            payment = (
                await db.execute(
                    select(Payment)
                    .where(Payment.id == payment_id, Payment.provider == "lava")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if payment is not None:
                return payment

    subscription_id = metadata.get("lava_subscription_id")
    if subscription_id:
        return (
            await db.execute(
                select(Payment)
                .where(
                    Payment.provider == "lava",
                    Payment.external_id == subscription_id,
                    Payment.status == "pending",
                )
                .order_by(Payment.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
    return None


async def _create_lava_payment_from_invoice(db: DB, event) -> Payment | None:
    metadata = event.metadata
    subscription_id = metadata.get("lava_subscription_id")
    local_sub = await _find_lava_subscription(db, subscription_id, lock=True)
    user_id = _metadata_int(metadata, "user_id")
    plan_id = _metadata_int(metadata, "plan_id")
    gift_recipient_id = _metadata_int(metadata, "gift_recipient_id")
    is_gift = _metadata_bool(metadata, "is_gift") or gift_recipient_id is not None

    if local_sub is not None:
        user_id = user_id or local_sub.user_id
        plan_id = plan_id or local_sub.plan_id

    if user_id is None or plan_id is None:
        return None

    payment = Payment(
        user_id=user_id,
        plan_id=plan_id,
        provider="lava",
        amount=Decimal(str(event.amount)),
        currency=event.currency,
        status="pending",
        is_gift=is_gift,
        gift_recipient_id=gift_recipient_id,
        is_renewal=_lava_invoice_is_renewal(metadata)
        or (local_sub is not None and _metadata_int(metadata, "payment_id") is None),
    )
    _bind_lava_invoice_fields(payment, event, status="succeeded")
    db.add(payment)
    await db.flush()
    return payment


def _bind_lava_invoice_fields(payment: Payment, event, *, status: str) -> None:
    metadata = event.metadata
    invoice_id = metadata.get("lava_invoice_id")
    subscription_id = metadata.get("lava_subscription_id")

    payment.status = status
    payment.provider = "lava"
    payment.amount = Decimal(str(event.amount))
    payment.currency = event.currency
    payment.provider_event_id = _lava_event_id(event)
    if invoice_id:
        payment.lava_invoice_id = invoice_id
    if subscription_id:
        payment.lava_subscription_id = subscription_id
        payment.external_id = subscription_id
    elif invoice_id and not payment.external_id:
        payment.external_id = invoice_id
    if _lava_invoice_is_renewal(metadata):
        payment.is_renewal = True

    period_start = _metadata_dt(metadata, "lava_period_start")
    period_end = _metadata_dt(metadata, "lava_period_end")
    if period_start is not None:
        payment.billing_period_start = period_start
    if period_end is not None:
        payment.billing_period_end = period_end


async def _find_lava_subscription(
    db: DB,
    subscription_id: str | None,
    *,
    lock: bool = False,
) -> Subscription | None:
    if not subscription_id:
        return None
    q = (
        select(Subscription)
        .where(
            Subscription.provider == "lava",
            Subscription.provider_subscription_id == subscription_id,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    if lock:
        q = q.with_for_update()
    return (await db.execute(q)).scalar_one_or_none()


def _lava_event_id(event) -> str:
    return str(event.metadata.get("lava_event_id") or event.external_id or "")


def _lava_amount_matches_payment(payment: Payment, event) -> bool:
    try:
        event_amount = Decimal(str(event.amount)).quantize(Decimal("0.01"))
        recorded = Decimal(str(payment.amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    currency = str(event.currency or "").upper()
    return event_amount == recorded and currency == str(payment.currency or "").upper()


async def _alert_unactionable_lava_charge(event, *, headline: str, detail: str) -> None:
    """Page a human about a Lava charge this handler could not act on (GK-416).

    Every branch that answers 200 without granting access is, from the member's
    side, "I paid and nothing happened". Two of them are reachable in production
    today: a recurring charge whose contract id matches no local subscription,
    and a paid amount that differs from what the bot recorded. Both used to
    return quietly, so the first sign of trouble was the member complaining.

    Rate-limited per contract rather than globally: a systemic breakage must not
    let the first victim's alert mask the next twenty.
    """
    metadata = event.metadata
    contract_id = metadata.get("lava_invoice_id") or event.external_id or "—"
    parent_id = metadata.get("lava_subscription_id") or "—"
    # GK-451: plain text, and no escaping here. `send_ops_alert` escapes the
    # whole body once on the way out, so the bold/monospace wrappers this used to
    # carry would arrive as literal tag characters and every field would be
    # escaped twice — `&` as `&amp;amp;`. Of all the alerts in the system this is
    # the one that must stay readable: it is the "a member paid and got nothing"
    # page, and the fields that would have been mangled are the contract ids
    # somebody has to paste into Lava's panel at 03:00.
    lines = [
        headline,
        detail,
        f"Сумма: {event.amount} {event.currency}",
        f"contractId: {contract_id}",
        f"parentContractId: {parent_id}",
        f"Событие: {metadata.get('lava_event_type') or event.status}",
    ]
    await send_ops_alert(
        "\n".join(lines),
        key=f"lava_unactionable_{contract_id}",
        rate_limit_seconds=3600,
        severity="error",
    )


def _lava_invoice_is_renewal(metadata: dict) -> bool:
    return _metadata_bool(metadata, "lava_is_renewal") or metadata.get("lava_event_type") in {
        "subscription.recurring.payment.success",
        "subscription.recurring.payment.failed",
    }
