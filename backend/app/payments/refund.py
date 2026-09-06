"""Provider-aware refund lifecycle (GK-200/GK-382).

Refund requests and confirmed money movement are intentionally separate. Only
``provider_confirmed`` calls :func:`_apply_confirmed_refund`, which is the sole
place allowed to update ``Payment.refunded_amount``, end access, adjust partner
commissions, or dispatch ``payment.refunded``.

Lifecycle:

``requested`` -> ``pending`` / ``manual_action_required`` ->
``provider_confirmed`` or ``failed``.

Stripe requests can confirm immediately from the provider response or remain
pending and be synchronized later. Lava/USDT/Zelle/manual refunds require an
explicit audited confirmation after the external action is complete.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Payment, Refund, utcnow
from app.services.referral_ledger import adjust_commission_for_refund
from app.services.subscription import end_access_for_refund
from app.services.webhooks import dispatch

logger = logging.getLogger(__name__)
settings = get_settings()

_MONEY = Decimal("0.01")

REFUND_REQUESTED = "requested"
REFUND_PENDING = "pending"
REFUND_CONFIRMED = "provider_confirmed"
REFUND_FAILED = "failed"
REFUND_MANUAL_ACTION = "manual_action_required"

ACTIVE_REFUND_STATUSES = {
    REFUND_REQUESTED,
    REFUND_PENDING,
    REFUND_MANUAL_ACTION,
}

REFUND_FULL = "full"
REFUND_PARTIAL = "partial"

# Providers whose money moves outside our rails -> manual confirmation only.
MANUAL_REFUND_PROVIDERS = {"usdt", "zelle", "manual"}


class RefundError(ValueError):
    """Raised for invalid refund operations; mapped to HTTP 400 by the router."""


@dataclass
class RefundResult:
    refund: Refund
    payment_status: str
    fully_refunded: bool
    refunded_total: Decimal
    commission_action: str
    commission_adjustment_usd: Decimal
    revoked_subscriptions: list = field(default_factory=list)
    state_changed: bool = True


class StripeRefundGateway(Protocol):
    async def create_refund(
        self,
        *,
        payment_intent_id: str | None,
        charge_id: str | None,
        amount_cents: int,
        reason: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        ...

    async def retrieve_refund(self, refund_id: str) -> dict[str, Any]:
        ...


class LiveStripeRefundGateway:
    """Real Stripe refund calls, run off the event loop."""

    async def create_refund(
        self,
        *,
        payment_intent_id: str | None,
        charge_id: str | None,
        amount_cents: int,
        reason: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        import stripe

        stripe.api_key = settings.stripe_secret_key
        params: dict[str, Any] = {
            "amount": amount_cents,
            "idempotency_key": idempotency_key,
        }
        if reason:
            params["reason"] = reason
        if payment_intent_id:
            params["payment_intent"] = payment_intent_id
        elif charge_id:
            params["charge"] = charge_id
        refund = await asyncio.to_thread(stripe.Refund.create, **params)
        return _stripe_refund_dict(refund)

    async def retrieve_refund(self, refund_id: str) -> dict[str, Any]:
        import stripe

        stripe.api_key = settings.stripe_secret_key
        refund = await asyncio.to_thread(stripe.Refund.retrieve, refund_id)
        return _stripe_refund_dict(refund)


async def create_refund(
    session: AsyncSession,
    payment: Payment,
    *,
    amount: Decimal | None = None,
    reason: str = "",
    admin_id: int | None = None,
    manual: bool = False,
    process: bool = True,
    stripe_gateway: StripeRefundGateway | None = None,
    now=None,
) -> RefundResult:
    """Create one refund request and optionally start provider processing.

    The caller must load ``payment.refunds`` while locking the payment row. The
    database also enforces one active refund per payment, so concurrent requests
    cannot over-reserve the remaining balance.
    """
    now = now or utcnow()
    if payment.status != "succeeded":
        raise RefundError("Only succeeded payments can be refunded")

    loaded_refunds = _loaded_refunds(payment)
    active = [r for r in loaded_refunds if getattr(r, "status", None) in ACTIVE_REFUND_STATUSES]
    if active:
        raise RefundError(
            f"Refund {active[0].id} is still {active[0].status}; resolve it before requesting another"
        )

    total = _money(payment.amount)
    already = _money(getattr(payment, "refunded_amount", 0) or 0)
    remaining = total - already
    if remaining <= 0:
        raise RefundError("Payment is already fully refunded")

    refund_amount = remaining if amount is None else _money(amount)
    if refund_amount <= 0:
        raise RefundError("Refund amount must be positive")
    if refund_amount > remaining:
        raise RefundError("Refund amount exceeds the remaining refundable balance")

    provider = (payment.provider or "").lower()
    reason_text = (reason or "").strip()
    is_manual = manual or provider in MANUAL_REFUND_PROVIDERS
    if is_manual and not reason_text:
        raise RefundError("Manual refund requests require an audit reason")
    if provider == "lava" and not is_manual:
        # GK-445: not "not enabled yet" — Lava has no refund endpoint to call,
        # and no flag will produce one. One honest message, because the person
        # reading it is in a hurry and a member is out of pocket.
        raise RefundError(
            "Lava has no refund API. Refund the payment in the Lava dashboard, "
            "then request a manual refund here and confirm it with the Lava "
            "reference."
        )
    if provider == "stripe" and not is_manual:
        _validate_stripe_payment(payment)

    new_total = _money(already + refund_amount)
    refund = Refund(
        payment_id=payment.id,
        provider=provider,
        amount=refund_amount,
        currency=payment.currency,
        refund_type=REFUND_FULL if new_total >= total else REFUND_PARTIAL,
        status=REFUND_REQUESTED,
        request_key=_request_key(payment, loaded_refunds, new_total),
        is_manual=is_manual,
        reason=reason_text[:1000] or None,
        created_by_admin_id=admin_id,
    )
    session.add(refund)
    if loaded_refunds is not None:
        loaded_refunds.append(refund)
    await session.flush()

    if not process:
        return _result(refund, payment)
    if is_manual:
        refund.status = REFUND_MANUAL_ACTION
        refund.provider_status = "manual_action_required"
        return _result(refund, payment)
    return await _process_stripe_refund(
        session,
        payment,
        refund,
        stripe_gateway=stripe_gateway,
        now=now,
    )


async def sync_provider_refund(
    session: AsyncSession,
    payment: Payment,
    refund: Refund,
    *,
    stripe_gateway: StripeRefundGateway | None = None,
    now=None,
) -> RefundResult:
    """Refresh an unresolved automatic refund from its provider.

    Replaying a terminal confirmation is an idempotent no-op. If the original
    Stripe request failed before returning an id, the same stored idempotency key
    is replayed so Stripe cannot create a second refund.
    """
    now = now or utcnow()
    if refund.payment_id != payment.id:
        raise RefundError("Refund does not belong to payment")
    if refund.status == REFUND_CONFIRMED and refund.accounting_applied_at is not None:
        return _result(refund, payment, state_changed=False)
    if refund.status == REFUND_FAILED:
        return _result(refund, payment, state_changed=False)
    if refund.is_manual or refund.provider != "stripe":
        raise RefundError("Only automatic Stripe refunds can be synchronized")
    return await _process_stripe_refund(
        session,
        payment,
        refund,
        stripe_gateway=stripe_gateway,
        now=now,
    )


async def resolve_manual_refund(
    session: AsyncSession,
    payment: Payment,
    refund: Refund,
    *,
    target_status: str,
    reason: str,
    admin_id: int,
    provider_reference: str | None = None,
    now=None,
) -> RefundResult:
    """Confirm or fail a manual/external refund with an audit trail."""
    now = now or utcnow()
    if refund.payment_id != payment.id:
        raise RefundError("Refund does not belong to payment")
    if not refund.is_manual:
        raise RefundError("Automatic provider refunds must be synchronized, not manually confirmed")
    if refund.status == REFUND_CONFIRMED and refund.accounting_applied_at is not None:
        return _result(refund, payment, state_changed=False)
    if refund.status == REFUND_FAILED and target_status == REFUND_FAILED:
        return _result(refund, payment, state_changed=False)
    if refund.status not in ACTIVE_REFUND_STATUSES:
        raise RefundError(f"Refund cannot transition from {refund.status}")

    reason_text = (reason or "").strip()
    if not reason_text:
        raise RefundError("Manual refund resolution requires an audit reason")
    reference = (provider_reference or "").strip() or None
    if target_status == REFUND_FAILED:
        refund.status = REFUND_FAILED
        refund.provider_status = "manual_failed"
        refund.failure_reason = reason_text[:1000]
        refund.confirmation_note = reason_text[:1000]
        refund.confirmed_by_admin_id = admin_id
        refund.confirmed_at = now
        return _result(refund, payment)
    if target_status != REFUND_CONFIRMED:
        raise RefundError("Manual refund status must be provider_confirmed or failed")

    if reference:
        refund.provider_refund_id = reference[:255]
    refund.provider_status = "manual_confirmed"
    refund.confirmation_note = reason_text[:1000]
    refund.confirmed_by_admin_id = admin_id
    return await _apply_confirmed_refund(
        session,
        payment,
        refund,
        admin_id=admin_id,
        confirmation_note=reason_text,
        now=now,
    )


async def _process_stripe_refund(
    session: AsyncSession,
    payment: Payment,
    refund: Refund,
    *,
    stripe_gateway: StripeRefundGateway | None,
    now,
) -> RefundResult:
    _validate_stripe_payment(payment)
    gateway = stripe_gateway or LiveStripeRefundGateway()
    try:
        if refund.provider_refund_id:
            provider_result = await gateway.retrieve_refund(refund.provider_refund_id)
        else:
            provider_result = await gateway.create_refund(
                payment_intent_id=payment.stripe_payment_intent_id,
                charge_id=None,
                amount_cents=_amount_cents(_money(refund.amount)),
                reason="requested_by_customer",
                idempotency_key=refund.request_key,
            )
    except Exception as exc:  # provider outcome may be unknown; retain for sync/retry
        logger.exception("Stripe refund request/sync failed for refund %s", refund.id)
        refund.status = REFUND_PENDING
        refund.provider_status = "request_error"
        refund.failure_reason = f"{type(exc).__name__}: {exc}"[:1000]
        return _result(refund, payment)

    provider_id = str(provider_result.get("id") or "").strip() or None
    provider_status = str(provider_result.get("status") or "").strip().lower()
    failure_reason = str(provider_result.get("failure_reason") or "").strip() or None
    if provider_id:
        refund.provider_refund_id = provider_id[:255]
    refund.provider_status = provider_status[:32] or None
    refund.failure_reason = failure_reason[:1000] if failure_reason else None

    if provider_status == "succeeded" and provider_id:
        refund.confirmed_by_admin_id = refund.created_by_admin_id
        return await _apply_confirmed_refund(
            session,
            payment,
            refund,
            admin_id=refund.created_by_admin_id,
            confirmation_note="Stripe provider confirmed refund",
            now=now,
        )
    if provider_status in {"failed", "canceled", "cancelled"}:
        refund.status = REFUND_FAILED
        if not refund.failure_reason:
            refund.failure_reason = f"Stripe refund status: {provider_status}"
        return _result(refund, payment)
    if provider_status == "requires_action":
        refund.status = REFUND_MANUAL_ACTION
        return _result(refund, payment)

    refund.status = REFUND_PENDING
    return _result(refund, payment)


async def _apply_confirmed_refund(
    session: AsyncSession,
    payment: Payment,
    refund: Refund,
    *,
    admin_id: int | None,
    confirmation_note: str,
    now,
) -> RefundResult:
    if refund.accounting_applied_at is not None:
        return _result(refund, payment, state_changed=False)

    total = _money(payment.amount)
    already = _money(getattr(payment, "refunded_amount", 0) or 0)
    refund_amount = _money(refund.amount)
    new_total = _money(already + refund_amount)
    if new_total > total:
        raise RefundError("Confirmed refund exceeds the remaining refundable balance")
    fully_refunded = new_total >= total

    refund.status = REFUND_CONFIRMED
    refund.confirmed_at = refund.confirmed_at or now
    refund.confirmed_by_admin_id = refund.confirmed_by_admin_id or admin_id
    refund.confirmation_note = refund.confirmation_note or confirmation_note[:1000]
    payment.refunded_amount = new_total

    revoked_subscriptions: list = []
    if fully_refunded:
        payment.status = "refunded"
        access_user_id = payment.gift_recipient_id if payment.is_gift else payment.user_id
        if access_user_id is not None:
            revoked_subscriptions = await end_access_for_refund(
                session,
                access_user_id,
                reason=(refund.reason or confirmation_note or "refund")[:200],
                now=now,
            )

    commission_action = "none"
    commission_adjustment = Decimal("0")
    if not payment.is_gift:
        commission_action, commission_adjustment = await adjust_commission_for_refund(
            session,
            payment,
            refund_amount=refund_amount,
            refunded_total=new_total,
            payment_total=total,
            fully_refunded=fully_refunded,
            reason=(refund.reason or confirmation_note or "refund")[:500],
            admin_id=admin_id,
            now=now,
        )
    refund.commission_action = commission_action
    refund.commission_adjustment_usd = (
        commission_adjustment if commission_adjustment != 0 else None
    )
    refund.accounting_applied_at = now

    await dispatch(
        session,
        "payment.refunded",
        {
            "payment_id": payment.id,
            "refund_id": refund.id,
            "provider": refund.provider,
            "amount": float(refund_amount),
            "currency": payment.currency,
            "refund_type": refund.refund_type,
            "fully_refunded": fully_refunded,
            "refunded_total": float(new_total),
            "is_manual": refund.is_manual,
            "commission_action": commission_action,
        },
    )
    return RefundResult(
        refund=refund,
        payment_status=payment.status,
        fully_refunded=fully_refunded,
        refunded_total=new_total,
        commission_action=commission_action,
        commission_adjustment_usd=commission_adjustment,
        revoked_subscriptions=revoked_subscriptions,
    )


def _result(refund: Refund, payment: Payment, *, state_changed: bool = True) -> RefundResult:
    total = _money(payment.amount)
    refunded = _money(getattr(payment, "refunded_amount", 0) or 0)
    adjustment = _money(getattr(refund, "commission_adjustment_usd", 0) or 0)
    return RefundResult(
        refund=refund,
        payment_status=payment.status,
        fully_refunded=refunded >= total and total > 0,
        refunded_total=refunded,
        commission_action=getattr(refund, "commission_action", None) or "none",
        commission_adjustment_usd=adjustment,
        state_changed=state_changed,
    )


def _loaded_refunds(payment: Payment) -> list[Refund]:
    # Avoid async lazy-loading. API callers eagerly load this relationship while
    # holding the payment row lock; unit-test stand-ins expose the same attribute.
    refunds = getattr(payment, "__dict__", {}).get("refunds")
    if refunds is None:
        refunds = []
        try:
            payment.refunds = refunds
        except (AttributeError, TypeError):
            pass
    return refunds


def _request_key(payment: Payment, refunds: list[Refund], new_total: Decimal) -> str:
    # Attempt ordinal allows a deliberate retry after a terminal failure, while a
    # DB rollback followed by the same request reproduces the same Stripe key.
    attempt = len(refunds) + 1
    return f"gk-refund-payment-{payment.id}-{attempt}-{_amount_cents(new_total)}"


def _validate_stripe_payment(payment: Payment) -> None:
    if not settings.stripe_secret_key:
        raise RefundError("Stripe is not configured; request a manual refund instead")
    if not getattr(payment, "stripe_payment_intent_id", None):
        raise RefundError(
            "Stripe payment intent is not on record; refund it in Stripe and request "
            "an audited manual confirmation."
        )


def _stripe_refund_dict(refund: Any) -> dict[str, Any]:
    return {
        "id": _get(refund, "id"),
        "status": _get(refund, "status"),
        "failure_reason": _get(refund, "failure_reason"),
    }


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _money(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _amount_cents(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
