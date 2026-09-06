import logging
from datetime import datetime
from decimal import Decimal

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentAdmin
from app.config import get_settings
from app.db.models import Payment, Plan, Refund, Subscription, User
from app.payments.fulfillment import fulfill_payment
from app.payments.refund import (
    ACTIVE_REFUND_STATUSES,
    MANUAL_REFUND_PROVIDERS,
    REFUND_CONFIRMED,
    REFUND_FAILED,
    RefundError,
    create_refund,
    resolve_manual_refund,
    sync_provider_refund,
)
from app.payments.usdt_verifier import verify_and_apply_usdt_payment
from app.services.audit import record as audit_record
from app.services.billing_notifications import (
    HOLD_HELP_LINE,
    notify_payment_succeeded,
    prelaunch_hold_applies_to,
)

router = APIRouter(prefix="/payments", tags=["payments"])
settings = get_settings()
logger = logging.getLogger(__name__)


class RefundOut(BaseModel):
    id: int
    amount: float
    currency: str
    refund_type: str
    status: str
    provider_status: str | None
    provider_reference: str | None
    is_manual: bool
    reason: str | None
    failure_reason: str | None
    confirmed_at: datetime | None
    accounting_applied: bool
    created_at: datetime


class PaymentOut(BaseModel):
    id: int
    user_id: int
    username: str | None
    plan_name: str | None
    provider: str
    amount: float
    currency: str
    status: str
    is_gift: bool
    gift_recipient_username: str | None
    external_id: str | None
    screenshot_url: str | None
    tx_hash: str | None
    tx_network: str | None
    tx_confirmed_at: datetime | None
    note: str | None
    created_at: datetime
    approved_at: datetime | None
    # GK-200 refund state for the admin UI.
    refunded_amount: float
    pending_refund_amount: float
    remaining_refundable_amount: float
    refund_state: str | None
    refunds: list[RefundOut]
    refundable: bool
    refund_mode: str  # auto | manual — default action for the refund button


class PaymentsPage(BaseModel):
    items: list[PaymentOut]
    total: int


def _refundable(payment: Payment) -> bool:
    """A succeeded payment with a remaining (un-refunded) balance can be refunded."""
    if payment.status != "succeeded":
        return False
    amount = Decimal(str(payment.amount or 0))
    refunded = Decimal(str(getattr(payment, "refunded_amount", 0) or 0))
    return refunded < amount and not any(
        r.status in ACTIVE_REFUND_STATUSES for r in _payment_refunds(payment)
    )


def _payment_refunds(payment: Payment) -> list[Refund]:
    return list(getattr(payment, "__dict__", {}).get("refunds") or [])


def _refund_out(refund: Refund) -> RefundOut:
    return RefundOut(
        id=refund.id,
        amount=float(refund.amount),
        currency=refund.currency,
        refund_type=refund.refund_type,
        status=refund.status,
        provider_status=refund.provider_status,
        provider_reference=refund.provider_refund_id,
        is_manual=refund.is_manual,
        reason=refund.reason,
        failure_reason=refund.failure_reason,
        confirmed_at=refund.confirmed_at,
        accounting_applied=refund.accounting_applied_at is not None,
        created_at=refund.created_at,
    )


def _refund_summary(payment: Payment) -> tuple[str | None, Decimal, Decimal, list[RefundOut]]:
    rows = sorted(
        _payment_refunds(payment),
        key=lambda r: (r.created_at, r.id),
        reverse=True,
    )
    pending = sum(
        (Decimal(str(r.amount)) for r in rows if r.status in ACTIVE_REFUND_STATUSES),
        Decimal("0"),
    )
    remaining = max(
        Decimal("0"),
        Decimal(str(payment.amount or 0))
        - Decimal(str(payment.refunded_amount or 0))
        - pending,
    )
    return (rows[0].status if rows else None, pending, remaining, [_refund_out(r) for r in rows])


def _refund_mode(payment: Payment) -> str:
    """Whether the refund hits a provider API ('auto') or is recorded manually."""
    provider = (payment.provider or "").lower()
    if provider in MANUAL_REFUND_PROVIDERS:
        return "manual"
    if provider == "stripe":
        if settings.stripe_secret_key and getattr(payment, "stripe_payment_intent_id", None):
            return "auto"
        return "manual"
    # GK-445: Lava is manual, unconditionally. There is no Lava refund endpoint,
    # so an "auto" button here can only produce a 400 at the worst moment.
    return "manual"


@router.get("", response_model=PaymentsPage)
async def list_payments(
    db: DB, _: CurrentAdmin,
    status: str | None = Query(
        None, pattern="^(pending|awaiting_review|succeeded|failed|refunded)$"
    ),
    provider: str | None = Query(None, pattern="^(stripe|lava|zelle|usdt|manual)$"),
    payment_id: int | None = Query(None, ge=1),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    base = (
        select(Payment, User, Plan)
        .options(selectinload(Payment.refunds))
        .join(User, User.id == Payment.user_id)
        .outerjoin(Plan, Plan.id == Payment.plan_id)
    )
    if status:
        base = base.where(Payment.status == status)
    if provider:
        base = base.where(Payment.provider == provider)
    if payment_id is not None:
        base = base.where(Payment.id == payment_id)
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0)
    rows = (await db.execute(base.order_by(Payment.created_at.desc()).limit(limit).offset(offset))).all()

    items: list[PaymentOut] = []
    for p, u, plan in rows:
        gift_username = None
        if p.is_gift and p.gift_recipient_id:
            gift_username = (
                await db.execute(select(User.username).where(User.id == p.gift_recipient_id))
            ).scalar_one_or_none()
        # Never trust screenshot_url for rendering — only allow our own /uploads/
        # or https URLs. Anything else becomes None so the admin UI shows the
        # "no screenshot" placeholder instead of an attacker-controlled <img src>.
        safe_screenshot = _safe_screenshot_url(p.screenshot_url)
        refund_state, pending_refund, remaining_refundable, refunds = _refund_summary(p)
        items.append(
            PaymentOut(
                id=p.id,
                user_id=u.id,
                username=u.username,
                plan_name=plan.name if plan else None,
                provider=p.provider,
                amount=float(p.amount),
                currency=p.currency,
                status=p.status,
                is_gift=p.is_gift,
                gift_recipient_username=gift_username,
                external_id=p.external_id,
                screenshot_url=safe_screenshot,
                tx_hash=p.tx_hash,
                tx_network=p.tx_network,
                tx_confirmed_at=p.tx_confirmed_at,
                note=p.note,
                created_at=p.created_at,
                approved_at=p.approved_at,
                refunded_amount=float(p.refunded_amount or 0),
                pending_refund_amount=float(pending_refund),
                remaining_refundable_amount=float(remaining_refundable),
                refund_state=refund_state,
                refunds=refunds,
                refundable=_refundable(p),
                refund_mode=_refund_mode(p),
            )
        )
    return PaymentsPage(items=items, total=total)


def _safe_screenshot_url(raw: str | None) -> str | None:
    """Whitelist screenshot URLs at API boundary.

    Allowed:
    - Server-local path under /uploads/ (we own it)
    - https:// URL (no javascript:, data:, file:, etc.)
    Anything else returns None.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("/uploads/") and ".." not in raw and len(raw) < 500:
        return raw
    if raw.lower().startswith("https://") and len(raw) < 500:
        return raw
    return None


async def _notify_payment_approved(
    db,
    payment: Payment,
    subscription: Subscription | None,
) -> bool:
    if not settings.bot_token:
        return False
    return await notify_payment_succeeded(db, payment, subscription)


class ManualDecision(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=500)


class USDTVerifyRequest(BaseModel):
    tx_hash: str = Field(..., min_length=20, max_length=200)


class USDTVerifyResponse(BaseModel):
    ok: bool
    decision: str
    reason: str
    message: str
    status: str
    tx_hash: str | None
    tx_network: str | None
    confirmations: int | None
    notify_delivered: bool = False


@router.post("/{payment_id}/verify-usdt", response_model=USDTVerifyResponse)
async def verify_usdt_payment(
    payment_id: int,
    payload: USDTVerifyRequest,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    payment = (
        await db.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if payment is None:
        raise HTTPException(404, "Payment not found")
    if payment.provider != "usdt":
        raise HTTPException(400, "Payment is not a USDT payment")
    if payment.status != "awaiting_review":
        raise HTTPException(400, "Payment is not pending review")

    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    try:
        result = await verify_and_apply_usdt_payment(
            db,
            payment,
            payload.tx_hash,
            bot=bot,
        )
    finally:
        if bot:
            await bot.session.close()

    notify_ok = False
    if result.is_valid:
        payment.approved_by = admin.id
        subscription = None
        if result.subscription_id is not None:
            subscription = (
                await db.execute(
                    select(Subscription).where(Subscription.id == result.subscription_id)
                )
            ).scalar_one_or_none()
        notify_ok = await _notify_payment_approved(db, payment, subscription)

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="payment.usdt_verify",
        target_type="payment",
        target_id=payment.id,
        details={
            "decision": result.decision,
            "reason": result.reason,
            "tx_hash": result.tx_hash,
            "tx_network": result.network,
            "confirmations": result.confirmations,
            "notify_ok": notify_ok,
        },
        request=request,
    )
    return USDTVerifyResponse(
        ok=result.is_valid,
        decision=result.decision,
        reason=result.reason,
        message=result.message,
        status=payment.status,
        tx_hash=payment.tx_hash,
        tx_network=payment.tx_network,
        confirmations=result.confirmations,
        notify_delivered=notify_ok,
    )


@router.post("/{payment_id}/moderate")
async def moderate_manual(
    payment_id: int,
    payload: ManualDecision,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    # FOR UPDATE: double-clicking "Approve" or two admins acting in parallel
    # must not double-grant subscription days. Lock the row first.
    p = (
        await db.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Payment not found")
    if p.status != "awaiting_review":
        raise HTTPException(400, "Payment is not pending review")

    if payload.decision == "approve":
        if p.provider == "usdt" and not (payload.reason or "").strip():
            raise HTTPException(400, "Manual USDT approval requires an audit reason")
        if p.provider == "usdt":
            p.note = (p.note or "") + (
                f"\n[manual usdt approve by admin#{admin.id}] {(payload.reason or '').strip()}"
            )
        p.status = "succeeded"
        p.approved_by = admin.id
        # Fulfill: create subscription + invite. fulfill_payment owns approved_at
        # as the idempotent local fulfillment marker.
        bot = Bot(token=settings.bot_token) if settings.bot_token else None
        sub = None
        try:
            sub = await fulfill_payment(db, bot, p)
        finally:
            if bot:
                await bot.session.close()
        # Notify user with centralized billing templates; delivery failure is logged there.
        notify_ok = await _notify_payment_approved(db, p, sub)
        await audit_record(
            db,
            actor_admin_id=admin.id,
            action="payment.approve",
            target_type="payment",
            target_id=p.id,
            details={"amount": float(p.amount), "currency": p.currency, "notify_ok": notify_ok},
            request=request,
        )
        return {"ok": True, "status": p.status, "notify_delivered": notify_ok}

    # reject
    reason = (payload.reason or "").strip() or "не указана"
    p.status = "failed"
    p.note = (p.note or "") + f"\n[reject by admin#{admin.id}] {reason}"
    notify_ok = False
    if settings.bot_token:
        from app.services.notifications import send_message
        user = (await db.execute(select(User).where(User.id == p.user_id))).scalar_one_or_none()
        if user:
            # GK-459: `/support` is absent from the dispatcher while GK-443's
            # hold is on, so telling a rejected payer to write it points them at
            # the заглушка. Send them straight to the handle the заглушка would
            # have given them.
            appeal = (
                HOLD_HELP_LINE
                if prelaunch_hold_applies_to(user.tg_id)
                else "Если это ошибка — напишите /support."
            )
            notify_ok = await send_message(
                user.tg_id,
                f"❌ Платёж не подтверждён. Причина: {reason}\n{appeal}",
            )
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="payment.reject",
        target_type="payment",
        target_id=p.id,
        details={"reason": reason, "notify_ok": notify_ok},
        request=request,
    )
    return {"ok": True, "status": p.status, "notify_delivered": notify_ok}


class RefundRequest(BaseModel):
    # Omit/null amount for a full refund of the remaining balance.
    amount: float | None = Field(default=None, gt=0, le=1_000_000)
    reason: str = Field(default="", max_length=1000)
    # Force a manual record even for an auto-capable provider (e.g. the admin
    # already refunded in the Stripe/Lava dashboard).
    manual: bool = False
    # Persist only the requested state. Stripe can be processed later via /sync;
    # manual/external requests can be resolved via /confirm.
    defer: bool = False


class RefundResponse(BaseModel):
    ok: bool
    refund_id: int
    payment_id: int
    refund_status: str  # requested|pending|manual_action_required|provider_confirmed|failed
    refund_type: str  # full | partial
    payment_status: str
    fully_refunded: bool
    amount: float
    refunded_total: float
    currency: str
    is_manual: bool
    commission_action: str  # none | cancelled | reduced | adjusted
    commission_adjustment_usd: float | None
    access_ended: bool = False  # subscription access closed by a full refund
    access_revoked_count: int = 0  # users removed from the Telegram channel now
    notify_delivered: bool = False
    provider_status: str | None = None
    provider_reference: str | None = None
    failure_reason: str | None = None
    confirmed_at: datetime | None = None
    accounting_applied: bool = False
    requires_manual_action: bool = False
    state_changed: bool = True


@router.post("/{payment_id}/refund", response_model=RefundResponse)
async def refund_payment(
    payment_id: int,
    payload: RefundRequest,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    # FOR UPDATE: two admins refunding the same payment in parallel must not
    # double-refund. Lock the row first, like manual moderation.
    p = (
        await db.execute(
            select(Payment)
            .options(selectinload(Payment.refunds))
            .where(Payment.id == payment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Payment not found")

    try:
        result = await create_refund(
            db,
            p,
            amount=Decimal(str(payload.amount)) if payload.amount is not None else None,
            reason=payload.reason,
            admin_id=admin.id,
            manual=payload.manual,
            process=not payload.defer,
        )
    except RefundError as exc:
        raise HTTPException(400, str(exc)) from exc

    access_revoked_count = await _revoke_refunded_channel_access(result.revoked_subscriptions)
    notify_ok = await _notify_payment_refunded(db, p, result)

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="payment.refund.request",
        target_type="payment",
        target_id=p.id,
        details={
            "refund_id": result.refund.id,
            "amount": float(result.refund.amount),
            "currency": p.currency,
            "refund_type": result.refund.refund_type,
            "refund_status": result.refund.status,
            "fully_refunded": result.fully_refunded,
            "is_manual": result.refund.is_manual,
            "commission_action": result.commission_action,
            "commission_adjustment_usd": float(result.commission_adjustment_usd)
            if result.commission_adjustment_usd
            else None,
            "access_ended": bool(result.revoked_subscriptions),
            "access_revoked_count": access_revoked_count,
            "reason": (payload.reason or "").strip() or None,
            "notify_ok": notify_ok,
        },
        request=request,
    )
    return _refund_response(
        result,
        payment=p,
        access_revoked_count=access_revoked_count,
        notify_ok=notify_ok,
    )


class RefundResolutionRequest(BaseModel):
    status: str = Field(pattern="^(provider_confirmed|failed)$")
    reason: str = Field(min_length=1, max_length=1000)
    provider_reference: str | None = Field(default=None, max_length=255)


async def _locked_refund(db: DB, refund_id: int) -> tuple[Payment, Refund]:
    refund_probe = (
        await db.execute(select(Refund).where(Refund.id == refund_id))
    ).scalar_one_or_none()
    if refund_probe is None:
        raise HTTPException(404, "Refund not found")
    payment = (
        await db.execute(
            select(Payment)
            .options(selectinload(Payment.refunds))
            .where(Payment.id == refund_probe.payment_id)
            .with_for_update()
        )
    ).scalar_one()
    refund = (
        await db.execute(select(Refund).where(Refund.id == refund_id).with_for_update())
    ).scalar_one()
    return payment, refund


@router.post("/refunds/{refund_id}/confirm", response_model=RefundResponse)
async def confirm_refund(
    refund_id: int,
    payload: RefundResolutionRequest,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    payment, refund = await _locked_refund(db, refund_id)
    try:
        result = await resolve_manual_refund(
            db,
            payment,
            refund,
            target_status=payload.status,
            reason=payload.reason,
            provider_reference=payload.provider_reference,
            admin_id=admin.id,
        )
    except RefundError as exc:
        raise HTTPException(400, str(exc)) from exc

    access_revoked_count = await _revoke_refunded_channel_access(result.revoked_subscriptions)
    notify_ok = await _notify_payment_refunded(db, payment, result)
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="payment.refund.resolve",
        target_type="refund",
        target_id=refund.id,
        details={
            "payment_id": payment.id,
            "status": refund.status,
            "provider_reference": refund.provider_refund_id,
            "accounting_applied": refund.accounting_applied_at is not None,
            "state_changed": result.state_changed,
            "reason": payload.reason,
        },
        request=request,
    )
    return _refund_response(
        result,
        payment=payment,
        access_revoked_count=access_revoked_count,
        notify_ok=notify_ok,
    )


@router.post("/refunds/{refund_id}/sync", response_model=RefundResponse)
async def sync_refund(
    refund_id: int,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    payment, refund = await _locked_refund(db, refund_id)
    try:
        result = await sync_provider_refund(db, payment, refund)
    except RefundError as exc:
        raise HTTPException(400, str(exc)) from exc

    access_revoked_count = await _revoke_refunded_channel_access(result.revoked_subscriptions)
    notify_ok = await _notify_payment_refunded(db, payment, result)
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="payment.refund.sync",
        target_type="refund",
        target_id=refund.id,
        details={
            "payment_id": payment.id,
            "status": refund.status,
            "provider_status": refund.provider_status,
            "accounting_applied": refund.accounting_applied_at is not None,
            "state_changed": result.state_changed,
        },
        request=request,
    )
    return _refund_response(
        result,
        payment=payment,
        access_revoked_count=access_revoked_count,
        notify_ok=notify_ok,
    )


def _refund_response(
    result,
    *,
    payment: Payment,
    access_revoked_count: int,
    notify_ok: bool,
) -> RefundResponse:
    return RefundResponse(
        ok=result.refund.status != REFUND_FAILED,
        refund_id=result.refund.id,
        payment_id=payment.id,
        refund_status=result.refund.status,
        refund_type=result.refund.refund_type,
        payment_status=result.payment_status,
        fully_refunded=result.fully_refunded,
        amount=float(result.refund.amount),
        refunded_total=float(result.refunded_total),
        currency=payment.currency,
        is_manual=result.refund.is_manual,
        commission_action=result.commission_action,
        commission_adjustment_usd=float(result.commission_adjustment_usd)
        if result.commission_adjustment_usd
        else None,
        access_ended=bool(result.revoked_subscriptions),
        access_revoked_count=access_revoked_count,
        notify_delivered=notify_ok,
        provider_status=getattr(result.refund, "provider_status", None),
        provider_reference=getattr(result.refund, "provider_refund_id", None),
        failure_reason=getattr(result.refund, "failure_reason", None),
        confirmed_at=getattr(result.refund, "confirmed_at", None),
        accounting_applied=getattr(result.refund, "accounting_applied_at", None) is not None,
        requires_manual_action=result.refund.status == "manual_action_required",
        state_changed=getattr(result, "state_changed", True),
    )


async def _revoke_refunded_channel_access(subscriptions: list) -> int:
    """Kick refund-revoked subscribers from the private channel now.

    DB state already blocks the portal; this removes Telegram access immediately.
    Each attempt is recorded on the subscription, so a failure (rate limit, etc.)
    leaves `access_revoked_at` NULL and the hourly kick job retries.
    """
    if not subscriptions or not settings.bot_token:
        return 0
    from app.services.channel_access import (
        SubscriptionAccessRevokeResult,
        revoke_subscription_access,
    )
    from app.services.subscription import record_access_revoke_attempt

    revoked = 0
    bot = Bot(token=settings.bot_token)
    try:
        for sub in subscriptions:
            user = getattr(sub, "user", None)
            tg_id = getattr(user, "tg_id", None)
            if tg_id is None:
                continue
            try:
                result = await revoke_subscription_access(
                    bot,
                    tg_id,
                    getattr(sub, "invite_link", None),
                )
            except Exception as e:
                logger.exception(
                    "refund access revoke crashed for subscription %s",
                    getattr(sub, "id", None),
                )
                result = SubscriptionAccessRevokeResult(False, error=str(e))
            record_access_revoke_attempt(
                sub,
                success=result.success,
                retry_after_seconds=result.retry_after,
                error=result.error,
            )
            if result.success:
                revoked += 1
    finally:
        await bot.session.close()
    return revoked


async def _notify_payment_refunded(db, payment: Payment, result) -> bool:
    """Best-effort Telegram notice to the buyer; failures are non-fatal."""
    if (
        not settings.bot_token
        or result.refund.status != REFUND_CONFIRMED
        or not getattr(result, "state_changed", True)
    ):
        return False
    from app.services.notifications import send_message

    user = (await db.execute(select(User).where(User.id == payment.user_id))).scalar_one_or_none()
    if not user:
        return False
    amount = float(result.refund.amount)
    scope = "Полный возврат" if result.fully_refunded else "Частичный возврат"
    # GK-459, and GK-444 saw this one coming: "that DM ends «напишите /support».
    # Under GK-443's hold, /support answers the заглушка… warn the curators
    # before issuing a refund during the hold window." Naming the curators
    # directly is the fix that makes the warning unnecessary.
    followup = (
        HOLD_HELP_LINE
        if prelaunch_hold_applies_to(user.tg_id)
        else "Если у вас есть вопросы — напишите /support."
    )
    return await send_message(
        user.tg_id,
        f"💸 {scope} оформлен на сумму {amount:.2f} {payment.currency}.\n{followup}",
    )
