from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import stripe
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Payment, Plan, PromoCode, User
from app.payments.base import CheckoutResult, PaymentEvent
from app.services.promo import DISCOUNT_FIXED, discount_for_checkout
from app.services.referral import REFERRAL_DISCOUNT_CODE, REFERRAL_DISCOUNT_PERCENT
from app.services.subscription import access_start_floor

logger = logging.getLogger(__name__)
settings = get_settings()
if settings.stripe_secret_key:
    stripe.api_key = settings.stripe_secret_key


class StripeWebhookVerificationError(Exception):
    """Raised when a Stripe webhook cannot be authenticated."""


class StripeCancellationError(RuntimeError):
    """Stripe refused or could not process an autorenew cancellation."""


class StripePriceMismatch(RuntimeError):
    """The configured Stripe Price does not charge what the bot displays.

    GK-422 happened exactly once and cost nothing only because somebody
    checked by hand: the 6m and 12m Price objects still charged $89/$149 after
    the plans were repriced to $79/$129, and **nothing in the product would
    ever have noticed**. Lava has had `_validate_invoice_total` guarding the
    same class of drift since GK-412; Stripe had no equivalent.

    Deliberately fail-closed: refusing the checkout is worse for one buyer than
    a silent overcharge is for the business's standing with every buyer. It
    ships with GK-421 rather than before it, so a mismatch surfaces as an
    honest message instead of another dead button.
    """


@dataclass(frozen=True)
class StripeRecurring:
    interval: str
    interval_count: int
    launch_code: str


class StripeProvider:
    name = "stripe"

    @staticmethod
    async def create_checkout(
        session: AsyncSession,
        user: User,
        plan: Plan,
        gift_recipient_id: int | None = None,
        *,
        is_gift: bool = False,
        promo_code: str | None = None,
    ) -> CheckoutResult:
        recurring = _recurring_for_plan(plan)
        amount = _money(plan.price_usd)
        is_gift = is_gift or gift_recipient_id is not None
        discount = await discount_for_checkout(
            session,
            user,
            plan,
            amount,
            "USD",
            promo_code=promo_code,
            is_gift=is_gift,
        )
        # The line item stays at full price; a discount is expressed as a
        # one-time Stripe coupon so subscription *renewals* are never discounted.

        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider="stripe",
            amount=discount.amount,
            currency="USD",
            status="pending",
            is_gift=is_gift,
            gift_recipient_id=gift_recipient_id,
        )
        session.add(payment)
        await session.flush()
        discount.link_payment(payment)

        if not settings.stripe_secret_key:
            # Demo / dev: return a stub URL that goes nowhere, instructing the user.
            return CheckoutResult(
                payment_id=payment.id,
                url=f"{settings.public_base_url}/dev/stripe-stub?payment_id={payment.id}",
                instructions=None,
            )

        customer_id = await _ensure_customer(user)
        if customer_id:
            user.stripe_customer_id = customer_id

        metadata = {
            "payment_id": str(payment.id),
            "user_id": str(user.id),
            "plan_id": str(plan.id),
            "plan_code": plan.code,
            "launch_plan": recurring.launch_code,
            "is_gift": str(is_gift).lower(),
            "gift_recipient_id": str(gift_recipient_id or ""),
        }
        if is_gift:
            metadata.update(
                {
                    "gift_term": recurring.launch_code,
                    "gift_duration_days": str(plan.duration_days),
                    "gift_list_price": f"{amount:.2f}",
                    "gift_currency": "USD",
                }
            )
        # GK-439 option 1 — «деньги в августе, первое продление 1 октября».
        # While the launch floor is set, Stripe must be told to charge again at
        # the END of the floored first period, not a plan-interval after
        # checkout; otherwise a member paying 19.08 has access to 01.10 and is
        # billed again on 19.09 — early, every month, forever.
        floored = None if is_gift else _floored_first_period(plan)
        if floored is not None:
            metadata["access_start_floor"] = floored[0].isoformat()
            metadata["first_renewal_at"] = floored[1].isoformat()

        discounts = None
        if discount.applied:
            if discount.kind == "promo" and discount.promo is not None:
                coupon_id = _ensure_promo_coupon(discount.promo)
                metadata["promo_code"] = discount.code or ""
            else:
                coupon_id = _ensure_referral_coupon()
                metadata["referral_discount"] = discount.code or REFERRAL_DISCOUNT_CODE
            discounts = [{"coupon": coupon_id}]

        checkout_params: dict[str, Any] = {
            "customer": customer_id,
            "payment_method_types": ["card"],
            "success_url": f"https://t.me/{settings.bot_username}?start=paid_{payment.id}",
            "cancel_url": f"https://t.me/{settings.bot_username}?start=cancel_{payment.id}",
            "client_reference_id": str(payment.id),
            "metadata": metadata,
            "idempotency_key": f"membership_saas-stripe-checkout-payment-{payment.id}",
        }
        if is_gift:
            # Gifts are fixed-duration access, never an auto-renewing charge.
            # Stripe recurring Price objects cannot be used in payment mode, so
            # create a one-time full-list-price line item from the launch plan.
            checkout_params.update(
                {
                    "mode": "payment",
                    "line_items": [
                        {
                            "price_data": {
                                "currency": "usd",
                                "unit_amount": _amount_cents(amount),
                                "product_data": {
                                    "name": f"Подарочная подписка: {plan.name}",
                                    "metadata": {
                                        "plan_id": str(plan.id),
                                        "gift_term": recurring.launch_code,
                                    },
                                },
                            },
                            "quantity": 1,
                        }
                    ],
                    "payment_intent_data": {"metadata": metadata},
                }
            )
        else:
            price_id = _price_id_for_plan(recurring)
            # GK-421/GK-422: refuse rather than charge an amount the member was
            # not shown. The line item is always full list price — a discount
            # rides along as a one-time coupon — so `amount` is what this Price
            # must equal.
            _validate_price_matches_plan(
                price_id,
                expected_amount=amount,
                recurring=recurring,
            )
            line_items: list[dict[str, Any]] = [{"price": price_id, "quantity": 1}]
            subscription_data: dict[str, Any] = {"metadata": metadata}
            if floored is not None:
                # The recurring Price stays in the session — Stripe requires one
                # in subscription mode, and it is what charges from the first
                # renewal onwards. What is bought *today* is the floored first
                # period, added as a one-time line item at full list price so
                # the existing coupon-based discount still applies to it exactly
                # once. `trial_end` then holds the recurring Price at zero until
                # that period ends.
                access_start, first_renewal = floored
                line_items.insert(
                    0,
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": _amount_cents(amount),
                            "product_data": {
                                "name": f"{plan.name}: доступ с {access_start:%d.%m.%Y}",
                                "metadata": {
                                    "plan_id": str(plan.id),
                                    "launch_plan": recurring.launch_code,
                                    "access_start_floor": access_start.isoformat(),
                                },
                            },
                        },
                        "quantity": 1,
                    },
                )
                subscription_data["trial_end"] = int(first_renewal.timestamp())
            checkout_params.update(
                {
                    "mode": "subscription",
                    "line_items": line_items,
                    "subscription_data": subscription_data,
                }
            )
            if discounts is not None:
                checkout_params["discounts"] = discounts

        sess = stripe.checkout.Session.create(**checkout_params)
        payment.stripe_checkout_session_id = sess.id
        payment.external_id = sess.id
        return CheckoutResult(payment_id=payment.id, url=sess.url, instructions=None)

    @staticmethod
    async def cancel_autorenew(subscription_id: str) -> None:
        """Stop future charges at the end of the paid period (GK-377).

        Deliberately ``cancel_at_period_end`` rather than an immediate delete:
        the member paid through a date and keeps access until it. Stripe echoes
        the change back as ``customer.subscription.updated``, which the existing
        webhook handler already reconciles.
        """
        if not settings.enable_stripe_autorenew_cancellation:
            raise StripeCancellationError(
                "Stripe autorenew cancellation is not enabled for this deployment"
            )
        if not subscription_id:
            raise StripeCancellationError("Stripe subscription id is missing")
        if not settings.stripe_secret_key:
            raise StripeCancellationError("Stripe API key is not configured")
        try:
            stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
        except Exception as exc:  # stripe raises a family of StripeError subclasses
            logger.warning(
                "stripe autorenew cancellation failed subscription_id=%s error=%s",
                subscription_id,
                exc,
            )
            raise StripeCancellationError(str(exc)) from exc

    @staticmethod
    def parse_webhook(payload: bytes, signature: str | None) -> PaymentEvent | None:
        if not settings.stripe_webhook_secret:
            logger.error("stripe webhook called but STRIPE_WEBHOOK_SECRET is not configured")
            raise StripeWebhookVerificationError("Stripe webhook secret is not configured")
        if not signature:
            logger.warning("stripe webhook missing Stripe-Signature header")
            raise StripeWebhookVerificationError("Missing Stripe-Signature header")
        try:
            data = stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
        except Exception as e:
            logger.warning("stripe webhook signature failed: %s", e)
            raise StripeWebhookVerificationError("Invalid Stripe webhook signature") from e

        event_type = _get(data, "type")
        event_id = _get(data, "id") or ""
        obj = _get(_get(data, "data") or {}, "object") or {}

        if event_type == "checkout.session.completed":
            return _parse_checkout_completed(obj, event_id)
        if event_type in {"invoice.paid", "invoice.payment_failed"}:
            return _parse_invoice_event(obj, event_id, event_type)
        if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
            return _parse_subscription_event(obj, event_id, event_type)
        return None


def _parse_checkout_completed(obj: Any, event_id: str) -> PaymentEvent:
    metadata = dict(_as_dict(_get(obj, "metadata")))
    metadata.update(
        {
            "stripe_event_id": event_id,
            "stripe_event_type": "checkout.session.completed",
            "stripe_checkout_session_id": _get(obj, "id") or "",
            "stripe_customer_id": _coerce_id(_get(obj, "customer")) or "",
            "stripe_subscription_id": _coerce_id(_get(obj, "subscription")) or "",
            "stripe_payment_intent_id": _coerce_id(_get(obj, "payment_intent")) or "",
            "stripe_payment_status": _get(obj, "payment_status") or "",
        }
    )
    return PaymentEvent(
        external_id=_get(obj, "id") or "",
        status="checkout_completed",
        amount=(_get(obj, "amount_total") or 0) / 100,
        currency=(_get(obj, "currency") or "usd").upper(),
        metadata=metadata,
    )


def _parse_invoice_event(obj: Any, event_id: str, event_type: str) -> PaymentEvent:
    line = _first_invoice_line(obj)
    line_period = _get(line, "period") or {}
    parent = _get(obj, "parent") or {}
    subscription_details = (
        _get(obj, "subscription_details") or _get(parent, "subscription_details") or {}
    )
    metadata = _merged_metadata(
        _get(line, "metadata"),
        _get(subscription_details, "metadata"),
        _get(obj, "metadata"),
    )
    subscription_id = (
        _coerce_id(_get(obj, "subscription"))
        or _coerce_id(_get(subscription_details, "subscription"))
        or _coerce_id(_get(parent, "subscription"))
    )
    period_start = _get(line_period, "start") or _get(obj, "period_start")
    period_end = _get(line_period, "end") or _get(obj, "period_end")
    amount_cents = _get(obj, "amount_paid")
    if amount_cents is None:
        amount_cents = _get(obj, "amount_due") or _get(obj, "amount_remaining") or 0
    metadata.update(
        {
            "stripe_event_id": event_id,
            "stripe_event_type": event_type,
            "stripe_invoice_id": _get(obj, "id") or "",
            "stripe_customer_id": _coerce_id(_get(obj, "customer")) or "",
            "stripe_subscription_id": subscription_id or "",
            "stripe_payment_intent_id": _coerce_id(_get(obj, "payment_intent")) or "",
            "stripe_invoice_status": _get(obj, "status") or "",
            "stripe_invoice_billing_reason": _get(obj, "billing_reason") or "",
            "stripe_invoice_period_start": str(period_start or ""),
            "stripe_invoice_period_end": str(period_end or ""),
        }
    )
    return PaymentEvent(
        external_id=_get(obj, "id") or "",
        status="invoice_paid" if event_type == "invoice.paid" else "invoice_payment_failed",
        amount=amount_cents / 100,
        currency=(_get(obj, "currency") or "usd").upper(),
        metadata=metadata,
    )


def _parse_subscription_event(obj: Any, event_id: str, event_type: str) -> PaymentEvent:
    metadata = dict(_as_dict(_get(obj, "metadata")))
    metadata.update(
        {
            "stripe_event_id": event_id,
            "stripe_event_type": event_type,
            "stripe_subscription_id": _get(obj, "id") or "",
            "stripe_customer_id": _coerce_id(_get(obj, "customer")) or "",
            "stripe_subscription_status": _get(obj, "status") or "",
            "stripe_subscription_current_period_start": str(_get(obj, "current_period_start") or ""),
            "stripe_subscription_current_period_end": str(_get(obj, "current_period_end") or ""),
            "stripe_subscription_cancel_at_period_end": str(
                bool(_get(obj, "cancel_at_period_end"))
            ).lower(),
        }
    )
    return PaymentEvent(
        external_id=_get(obj, "id") or "",
        status="subscription_deleted"
        if event_type == "customer.subscription.deleted"
        else "subscription_updated",
        amount=0,
        currency="USD",
        metadata=metadata,
    )


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "get"):
        try:
            return obj.get(key, default)
        except TypeError:
            pass
    return getattr(obj, key, default)


def _as_dict(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict_recursive"):
        return dict(value.to_dict_recursive())
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _merged_metadata(*sources: Any) -> dict:
    merged: dict = {}
    for source in sources:
        merged.update(_as_dict(source))
    return merged


def _coerce_id(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value
    return _get(value, "id")


def _first_invoice_line(obj: Any) -> Any:
    lines = _get(obj, "lines") or {}
    data = _get(lines, "data") or []
    return data[0] if data else {}


def _money(value: object) -> Decimal:
    return Decimal(str(value))


def _amount_cents(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _recurring_for_plan(plan: Plan) -> StripeRecurring:
    code = (plan.code or "").lower()
    duration_days = int(plan.duration_days)
    if code in {"1m", "month", "monthly"} or 28 <= duration_days <= 31:
        return StripeRecurring(interval="month", interval_count=1, launch_code="1m")
    if code in {"6m", "half_year", "semiannual"} or 175 <= duration_days <= 186:
        return StripeRecurring(interval="month", interval_count=6, launch_code="6m")
    if code in {"12m", "year", "annual"} or 360 <= duration_days <= 370:
        return StripeRecurring(interval="year", interval_count=1, launch_code="12m")
    raise ValueError(
        f"Unsupported Stripe subscription plan duration: code={plan.code!r}, "
        f"duration_days={plan.duration_days!r}"
    )


def _floored_first_period(plan: Plan) -> tuple[datetime, datetime] | None:
    """GK-439 option 1: ``(access start, first renewal)`` while the floor is on.

    Reads the *same* helper the access clock reads
    (`app.services.subscription.access_start_floor`) and applies the same
    arithmetic, so Stripe's next-charge date and our `expires_at` come from one
    decision rather than two that can drift. Returns ``None`` when the floor is
    unset or already past — checkout then keeps exactly today's shape, which is
    what makes "with the floor unset every existing test passes unchanged" true
    of the provider half as well as the local one.
    """
    floor = access_start_floor()
    if floor is None:
        return None
    return floor, floor + timedelta(days=int(plan.duration_days))


def _price_id_for_plan(recurring: StripeRecurring) -> str:
    price_ids = {
        "1m": settings.stripe_price_monthly_id,
        "6m": settings.stripe_price_6m_id,
        "12m": settings.stripe_price_annual_id,
    }
    price_id = (price_ids.get(recurring.launch_code) or "").strip()
    if not price_id:
        raise ValueError(f"Stripe Price ID is not configured for launch plan {recurring.launch_code}")
    if not price_id.startswith("price_"):
        raise ValueError(f"Configured Stripe Price ID for {recurring.launch_code} must start with 'price_'")
    return price_id


#: Stripe Price objects are immutable in amount/currency/recurring, but the Plan
#: expectations in our database are mutable. Cache only an exact comparison so
#: changing a plan amount or billing interval forces a fresh validation.
_VALIDATED_PRICE_EXPECTATIONS: set[tuple[str, Decimal, str, int]] = set()


def _validate_price_matches_plan(
    price_id: str,
    *,
    expected_amount: Decimal,
    recurring: StripeRecurring,
) -> None:
    """Raise StripePriceMismatch unless Stripe charges what the plan shows."""
    validation_key = (
        price_id,
        expected_amount,
        recurring.interval,
        recurring.interval_count,
    )
    if validation_key in _VALIDATED_PRICE_EXPECTATIONS:
        return
    try:
        price = stripe.Price.retrieve(price_id)
    except Exception as exc:  # network, bad key, deleted price
        raise StripePriceMismatch(
            f"could not verify Stripe price {price_id}: {exc.__class__.__name__}: {exc}"
        ) from exc

    problems: list[str] = []

    unit_amount = price.get("unit_amount")
    if unit_amount is None:
        problems.append("price has no unit_amount (tiered or metered price?)")
    else:
        actual = (Decimal(unit_amount) / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if actual != expected_amount:
            problems.append(f"charges {actual} but the plan shows {expected_amount}")

    currency = str(price.get("currency") or "").lower()
    if currency != "usd":
        problems.append(f"currency is {currency or 'unset'}, expected usd")

    price_recurring = price.get("recurring") or {}
    interval = str(price_recurring.get("interval") or "")
    interval_count = price_recurring.get("interval_count")
    if interval != recurring.interval or interval_count != recurring.interval_count:
        problems.append(
            f"bills every {interval_count} {interval or 'unset'}, "
            f"expected every {recurring.interval_count} {recurring.interval}"
        )

    if not price.get("active", True):
        problems.append("price is archived (active=false)")

    if problems:
        raise StripePriceMismatch(
            f"Stripe price {price_id} for plan {recurring.launch_code}: " + "; ".join(problems)
        )

    _VALIDATED_PRICE_EXPECTATIONS.add(validation_key)


async def _ensure_customer(user: User) -> str | None:
    stale_customer_id: str | None = None
    if user.stripe_customer_id:
        # GK-421: a stored id can point at a customer that was deleted in the
        # Stripe dashboard — that is the literal cause of the 28.07 incident
        # (`No such customer: cus_UfnIz3B6Xr9bOn`, six times in two minutes),
        # which was fixed by hand by nulling the column. Verify and re-create
        # instead, so the same mistake self-heals rather than needing a script.
        try:
            existing = stripe.Customer.retrieve(user.stripe_customer_id)
        except Exception:
            logger.warning(
                "stored stripe_customer_id could not be retrieved; recreating user_id=%s customer=%s",
                user.id,
                user.stripe_customer_id,
                exc_info=True,
            )
            stale_customer_id = user.stripe_customer_id
        else:
            if not existing.get("deleted"):
                return user.stripe_customer_id
            logger.warning(
                "stored stripe_customer_id is deleted at Stripe; recreating user_id=%s customer=%s",
                user.id,
                user.stripe_customer_id,
            )
            stale_customer_id = user.stripe_customer_id

    # The idempotency key must differ from the one that produced the stale
    # customer, or Stripe replays the cached response for 24h and hands back
    # the very customer we just rejected. Keying on the stale id keeps the call
    # deterministic (a retry recreates once, not once per attempt).
    idempotency_key = f"membership_saas-stripe-customer-user-{user.id}"
    if stale_customer_id:
        idempotency_key = f"{idempotency_key}-after-{stale_customer_id}"

    customer = stripe.Customer.create(
        name=_customer_name(user),
        metadata={"user_id": str(user.id), "tg_id": str(user.tg_id)},
        idempotency_key=idempotency_key,
    )
    return customer.id


def _customer_name(user: User) -> str | None:
    parts = [user.first_name, user.last_name]
    name = " ".join(part for part in parts if part)
    if name:
        return name
    return f"@{user.username}" if user.username else None


#: Stripe rejects a coupon `name` longer than this. GK-447: the limit is not in
#: our tests because every test mocks `Coupon.create`, so the only thing that has
#: ever enforced it is Stripe — and it enforces it at the moment a member is
#: standing at a checkout, not at deploy time. `_coupon_name` is the guard.
STRIPE_COUPON_NAME_MAX = 40


def _coupon_name(name: str) -> str:
    """Clamp a coupon name to Stripe's limit rather than letting it 400.

    A truncated name is cosmetic — it shows on the invoice line and nowhere a
    decision is made. A rejected `Coupon.create` is not: it raises out of
    `create_checkout`, so the member is told the checkout failed and the sale is
    lost. Given the choice, lose the tail of a label.
    """
    return name if len(name) <= STRIPE_COUPON_NAME_MAX else name[: STRIPE_COUPON_NAME_MAX - 1] + "…"


def _ensure_referral_coupon() -> str:
    coupon_id = settings.stripe_referral_coupon_id
    try:
        stripe.Coupon.retrieve(coupon_id)
    except stripe.error.InvalidRequestError as exc:
        if getattr(exc, "http_status", None) != 404:
            raise
        stripe.Coupon.create(
            id=coupon_id,
            percent_off=float(REFERRAL_DISCOUNT_PERCENT),
            duration="once",
            # GK-447. This read «membership_saas referral discount: first monthly
            # invoice» — 56 characters against Stripe's 40 — so the create call
            # 400'd and took `create_checkout` down with it. The coupon is made
            # lazily on the *first* referral checkout, and there has never been
            # one on the live account, so nothing had ever executed this line
            # against Stripe. Found by probing the live account before launch.
            name=_coupon_name("Referral: 20% off the first month"),
            metadata={"scope": "monthly_first_invoice_only"},
            idempotency_key=f"membership_saas-stripe-coupon-{coupon_id}",
        )
    return coupon_id


def _ensure_promo_coupon(promo: PromoCode) -> str:
    """Reuse-or-create a one-time Stripe coupon for ``promo``.

    The coupon id encodes the discount value so that editing a promo's discount
    yields a *fresh* coupon id (the stale one simply lingers, unused). All promo
    coupons are ``duration='once'`` — a promo discounts only the first invoice,
    never recurring renewals.
    """
    if promo.discount_type == DISCOUNT_FIXED:
        cents = _amount_cents(_money(promo.amount_off or 0))
        currency = (promo.amount_off_currency or "USD").lower()
        coupon_id = f"gk-promo-{promo.id}-{currency}{cents}"
        create_kwargs = {"amount_off": cents, "currency": currency}
    else:
        percent = float(promo.percent_off or 0)
        coupon_id = f"gk-promo-{promo.id}-p{int(round(percent * 100))}"
        create_kwargs = {"percent_off": percent}

    try:
        stripe.Coupon.retrieve(coupon_id)
    except stripe.error.InvalidRequestError as exc:
        if getattr(exc, "http_status", None) != 404:
            raise
        stripe.Coupon.create(
            id=coupon_id,
            duration="once",
            # GK-447: same clamp. "membership_saas promo " is already 22 of the
            # 40, so any promo code longer than 18 characters would have failed
            # the same way the referral one did — an admin naming a promo
            # `LAUNCH-SEPTEMBER-2026` is not a bug on their part.
            name=_coupon_name(f"membership_saas promo {promo.code}"),
            metadata={"promo_code": promo.code, "promo_id": str(promo.id)},
            idempotency_key=f"membership_saas-stripe-coupon-{coupon_id}",
            **create_kwargs,
        )
    return coupon_id
