"""Lava Top provider integration.

GK-060 validated the public Lava contract enough to implement local webhook
handling, but live checkout still waits for the real account API key and webhook
credential. Live invoice creation is additionally gated by the verified offer
ID so dropping a secret into .env cannot accidentally switch production
behavior.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Payment, Plan, User
from app.observability import send_ops_alert
from app.payments.base import CheckoutResult, PaymentEvent
from app.services.promo import discount_for_checkout

logger = logging.getLogger(__name__)
settings = get_settings()

_LAVA_EVENTS = {
    "payment.success": "invoice_paid",
    "payment.failed": "invoice_payment_failed",
    "subscription.recurring.payment.success": "invoice_paid",
    "subscription.recurring.payment.failed": "invoice_payment_failed",
    "subscription.cancelled": "subscription_deleted",
}
_LAVA_RENEWAL_EVENTS = {
    "subscription.recurring.payment.success",
    "subscription.recurring.payment.failed",
}
_LAVA_CREATE_INVOICE_PATH = "/api/v3/invoice"
_LAVA_PRODUCTS_PATH = "/api/v2/products"
# GK-377. Present in the official OpenAPI spec (gate.lava.top/docs/documentation.yaml)
# and takes the contract id. The exact request shape has NOT been exercised against
# the live account yet, which is why `enable_lava_autorenew_cancellation` defaults to
# False — the gated contract smoke must confirm both the path shape and that our API
# key carries the permission before this is switched on in production.
_LAVA_SUBSCRIPTION_PATH = "/api/v1/subscriptions"
# The buyer-facing offer page. GK-412 routed RUB checkout here; GK-418 reverted
# that — sales made on this page produce no webhook and no seller-API record, so
# they can never be fulfilled. Reachable only with ENABLE_LAVA_RUB_OFFER_PAGE=true,
# kept as a rollback lever and as documentation of the dead end.
_LAVA_PAGE_BASE_URL = "https://app.lava.top"


class LavaAPIError(RuntimeError):
    """Lava rejected a request or returned a malformed response."""


class LavaCheckoutUnavailable(RuntimeError):
    """The selected checkout cannot be represented by the verified Lava offer."""


class LavaCancellationUnavailable(RuntimeError):
    """Lava autorenew cancellation cannot be attempted through the API.

    Raised when the feature is still gated off or when we hold no contract id
    for the subscription. The missing-contract case is not hypothetical: every
    payment made through the GK-412 offer-page links (15-23.07) produced no
    purchase webhook, so `payment.lava_subscription_id` is NULL for them and the
    contract is invisible to the API entirely. GK-418 put RUB back on API
    invoices, which store the contract id at creation time, but those older rows
    remain. Callers must fall back to the manual queue — never to a "cancelled"
    claim.
    """


class LavaProvider:
    name = "lava"

    @staticmethod
    async def create_checkout(
        session: AsyncSession,
        user: User,
        plan: Plan,
        gift_recipient_id: int | None = None,
        *,
        is_gift: bool = False,
        promo_code: str | None = None,
        buyer_email: str | None = None,
    ) -> CheckoutResult:
        is_gift = is_gift or gift_recipient_id is not None
        base_amount, currency = _checkout_money_for_plan(plan)
        periodicity: str | None = None

        if settings.enable_lava_live_checkout:
            _validate_live_checkout_settings()
            periodicity = _periodicity_for_plan(plan)
            if not buyer_email:
                raise LavaCheckoutUnavailable("Lava Top requires the buyer's email")
            if is_gift:
                raise LavaCheckoutUnavailable(
                    "Lava gifts require a separate one-time offer; use Stripe or USDT"
                )

        discount = await discount_for_checkout(
            session,
            user,
            plan,
            base_amount,
            currency,
            promo_code=promo_code,
            is_gift=is_gift,
        )

        # The verified Lava offer has fixed prices. Its API accepts an amount
        # override only for dynamic-price products, so charging this offer after
        # applying a local referral/promo discount would silently overcharge.
        # Roll back a promo redemption recorded by discount_for_checkout and
        # direct the user to a provider that can honor the local discount.
        if settings.enable_lava_live_checkout and discount.applied:
            await session.rollback()
            raise LavaCheckoutUnavailable(
                "Lava Top cannot apply this local discount; use Stripe or USDT"
            )

        note = None
        if discount.applied:
            note = f"{discount.kind}_discount={discount.code}"

        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider="lava",
            amount=discount.amount,
            currency=currency,
            status="pending",
            is_gift=is_gift,
            gift_recipient_id=gift_recipient_id,
            note=note,
        )
        session.add(payment)
        await session.flush()
        discount.link_payment(payment)

        if not settings.enable_lava_live_checkout:
            return CheckoutResult(
                payment_id=payment.id,
                url=f"{settings.public_base_url}/dev/lava-stub?payment_id={payment.id}",
                instructions=(
                    "Lava live checkout is disabled. Set ENABLE_LAVA_LIVE_CHECKOUT=true "
                    "only after the Lava API key, product contract, and webhook secret "
                    "are verified for this environment."
                ),
            )

        # GK-377 reads the Lava buyer email back out of the note to cancel the
        # recurring contract (DELETE /api/v1/subscriptions takes contractId AND
        # email), and it is frequently not the member's account email — so it
        # must be recorded for every live checkout, not only the offer-page one
        # that first needed it. Without this a cancellation request can only
        # fall back to the manual queue.
        payment.note = _append_note(payment.note, f"buyer_email={buyer_email}")

        # GK-418: RUB falls through to the same /api/v3/invoice flow as USD/EUR.
        # GK-412 had routed it to the bare offer page (an API-created invoice
        # pins provider=smart_glocal into the link, and some RU cards hang on
        # that widget), but Lava confirmed in writing on 2026-07-23 that
        # platform/offer-page sales send NO purchase webhook — the capability
        # does not exist, only a feature request — and never appear in the
        # seller API either. So that link takes the money and grants nothing,
        # invisible to both push and pull reconciliation; it happened for real
        # on 2026-07-16. The invoice flow is the only one proven end-to-end on
        # real money (payments #61/#63, 2026-07-06). The offer-page code stays
        # behind a default-off flag as a rollback lever, not as a fix for the
        # "Оплатить" hang — that failure is flow-independent.
        if currency == "RUB" and settings.enable_lava_rub_offer_page:
            try:
                page_url = await _rub_offer_page_url(payment, periodicity, user)
            except (LavaAPIError, LavaCheckoutUnavailable):
                await session.rollback()
                raise
            return CheckoutResult(
                payment_id=payment.id,
                url=page_url,
                instructions=None,
            )

        # GK-482. The payload below deliberately carries no amount, so Lava's
        # catalogue — not ours — decides what leaves the card. Ask the catalogue
        # what that is and refuse before creating the invoice, rather than
        # discovering the divergence from the response (or never).
        try:
            await _verified_offer(payment, periodicity=periodicity, currency=currency)
        except (LavaAPIError, LavaCheckoutUnavailable):
            await session.rollback()
            raise

        payload = {
            "email": buyer_email,
            "offerId": settings.lava_offer_id.strip(),
            "currency": currency,
            "periodicity": periodicity,
            "buyerLanguage": _buyer_language(user),
            "clientUtm": {
                "utm_source": "telegram",
                "utm_medium": "bot",
                "utm_campaign": "membership_saas",
                # Lava returns clientUtm in webhooks. This lets the webhook
                # recover the local payment if the DB commit is interrupted
                # after Lava created the external contract.
                "utm_content": f"payment_{payment.id}",
            },
        }
        try:
            invoice = await _post_lava_invoice(payload)
            contract_id = _first_text(
                invoice,
                "id",
                "contractId",
                "contract_id",
                "invoiceId",
                "invoice_id",
                "data.id",
                "data.contractId",
                "data.contract_id",
                "data.invoiceId",
                "data.invoice_id",
            ) or ""
            payment_url = _first_text(
                invoice,
                "paymentUrl",
                "payment_url",
                "url",
                "data.paymentUrl",
                "data.payment_url",
                "data.url",
            ) or ""
            if not contract_id or not payment_url:
                raise LavaAPIError("Lava create-invoice response is missing id or paymentUrl")
            _validate_invoice_total(
                invoice,
                expected_amount=payment.amount,
                expected_currency=currency,
            )
        except LavaAPIError:
            await session.rollback()
            raise

        payment.lava_invoice_id = contract_id
        payment.lava_subscription_id = contract_id
        payment.external_id = contract_id
        return CheckoutResult(
            payment_id=payment.id,
            url=payment_url,
            instructions=None,
        )

    @staticmethod
    async def cancel_autorenew(contract_id: str | None, *, email: str | None) -> None:
        """Stop a Lava recurring contract from charging again (GK-377).

        Lava documents cancellation as ``DELETE /api/v1/subscriptions`` with
        ``contractId`` and ``email`` as *required query parameters*. The
        ``/{id}`` route in the same spec is GET-only, so addressing the contract
        in the path answers 404 — which this method reports as "contract
        unknown", the very fallback a genuine platform sale produces. Getting
        the shape wrong therefore fails silently, which is why it is asserted in
        tests rather than left to review.

        Raises LavaCancellationUnavailable when the call cannot be attempted at
        all (gated off, unconfigured, or missing contract id / buyer email) and
        LavaAPIError when Lava rejects it. The caller must treat both as "not
        cancelled".
        """
        if not settings.enable_lava_autorenew_cancellation:
            raise LavaCancellationUnavailable(
                "Lava autorenew cancellation is not enabled for this deployment"
            )
        if not contract_id:
            raise LavaCancellationUnavailable("Lava contract id is unknown")
        buyer_email = (email or "").strip()
        if not buyer_email:
            # Required by the API, and we must not guess: the Lava buyer email
            # is frequently not the member's account email.
            raise LavaCancellationUnavailable("Lava buyer email is unknown")
        if not settings.lava_api_key.strip():
            raise LavaCancellationUnavailable("Lava API key is not configured")

        base_url = settings.lava_api_base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
                response = await client.delete(
                    _LAVA_SUBSCRIPTION_PATH,
                    params={"contractId": contract_id, "email": buyer_email},
                    headers={"X-Api-Key": settings.lava_api_key.strip()},
                )
        except httpx.RequestError as exc:
            raise LavaAPIError("Lava cancel-subscription request failed") from exc

        # 404 means Lava does not know this contract — for the platform/offer-page
        # sales that never produced a purchase webhook this is the expected answer,
        # and it is a manual-queue case, not a success.
        if response.status_code == 404:
            raise LavaCancellationUnavailable(
                f"Lava does not know contract {contract_id}; cancel it from the dashboard"
            )
        if not 200 <= response.status_code < 300:
            logger.warning(
                "lava cancel subscription rejected status=%s contract_id=%s",
                response.status_code,
                contract_id,
            )
            raise LavaAPIError(
                f"Lava cancel-subscription returned HTTP {response.status_code}"
            )

    @staticmethod
    def verify_webhook_auth(
        *,
        x_api_key: str | None = None,
        authorization: str | None = None,
    ) -> bool:
        mode = (settings.lava_webhook_auth_mode or "api_key").strip().lower()
        if mode in {"api_key", "x_api_key", "x-api-key"}:
            return _verify_x_api_key(x_api_key)
        if mode == "basic":
            return _verify_basic_authorization(authorization)
        logger.error("lava webhook rejected: unsupported auth mode %s", mode)
        return False

    @staticmethod
    def parse_webhook(raw_body: bytes) -> PaymentEvent | None:
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            logger.warning("lava webhook bad json: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None

        event_type = _normalize_event_type(
            _first_value(payload, "event", "event_type", "eventType", "type")
        )
        if event_type not in _LAVA_EVENTS:
            event_type = _infer_event_type(payload)
        status = _LAVA_EVENTS.get(event_type)
        if status is None:
            logger.info("lava webhook ignored unsupported event_type=%r", event_type)
            return None

        raw_hash = hashlib.sha256(raw_body).hexdigest()
        invoice_id = _first_text(
            payload,
            "invoice_id",
            "invoiceId",
            "payment.id",
            "paymentId",
            "payment_id",
            "contract.id",
            "contractId",
            "contract_id",
        )
        subscription_id = _first_text(
            payload,
            "subscription_id",
            "subscriptionId",
            "subscription.id",
            "contract.subscriptionId",
            "contract.subscription_id",
            "parentContractId",
            "parent_contract_id",
            "parentContract.id",
            "parent_contract.id",
        )
        event_id = _first_text(
            payload,
            "event_id",
            "eventId",
            "webhook_event_id",
            "webhookEventId",
        )
        if not event_id:
            event_id = f"{event_type}:{invoice_id or subscription_id or raw_hash}"

        metadata = {
            "lava_event_id": event_id,
            "lava_event_type": event_type,
            "lava_invoice_id": invoice_id or "",
            "lava_subscription_id": subscription_id or invoice_id or "",
            "lava_raw_sha256": raw_hash,
            "lava_is_renewal": str(event_type in _LAVA_RENEWAL_EVENTS).lower(),
            "lava_contract_status": _first_text(
                payload,
                "status",
                "contract.status",
                "payment.status",
                "subscription.status",
            )
            or "",
            "payment_id": _first_text(
                payload,
                "metadata.payment_id",
                "metadata.paymentId",
                "custom_data.payment_id",
                "customData.payment_id",
                "order_id",
                "orderId",
                "external_id",
                "externalId",
            )
            or _payment_id_from_utm(payload)
            or "",
            "user_id": _first_text(
                payload,
                "metadata.user_id",
                "metadata.userId",
                "custom_data.user_id",
                "customData.userId",
            )
            or "",
            "plan_id": _first_text(
                payload,
                "metadata.plan_id",
                "metadata.planId",
                "custom_data.plan_id",
                "customData.planId",
            )
            or "",
            "is_gift": _first_text(
                payload,
                "metadata.is_gift",
                "metadata.isGift",
                "custom_data.is_gift",
                "customData.isGift",
            )
            or "",
            "gift_recipient_id": _first_text(
                payload,
                "metadata.gift_recipient_id",
                "metadata.giftRecipientId",
                "custom_data.gift_recipient_id",
                "customData.giftRecipientId",
            )
            or "",
            "lava_period_start": _first_text(
                payload,
                "period.start",
                "periodStart",
                "period_start",
                "currentPeriodStart",
                "current_period_start",
                "subscription.currentPeriodStart",
                "subscription.current_period_start",
            )
            or "",
            "lava_period_end": _first_text(
                payload,
                "period.end",
                "periodEnd",
                "period_end",
                "currentPeriodEnd",
                "current_period_end",
                "subscription.currentPeriodEnd",
                "subscription.current_period_end",
                "willExpireAt",
            )
            or "",
            "lava_cancel_at_period_end": _first_text(
                payload,
                "cancelAtPeriodEnd",
                "cancel_at_period_end",
                "subscription.cancelAtPeriodEnd",
                "subscription.cancel_at_period_end",
            )
            or ("true" if event_type == "subscription.cancelled" else ""),
        }
        amount = _first_decimal(
            payload,
            "amount",
            "amount_total",
            "amountTotal",
            "payment.amount",
            "contract.amount",
            "subscription.amount",
        )
        currency = (
            _first_text(
                payload,
                "currency",
                "payment.currency",
                "contract.currency",
                "subscription.currency",
            )
            or "RUB"
        ).upper()
        return PaymentEvent(
            external_id=invoice_id or subscription_id or event_id,
            status=status,
            amount=float(amount or Decimal("0")),
            currency=currency,
            metadata=metadata,
        )


def _checkout_money_for_plan(plan: Plan) -> tuple[Decimal, str]:
    rub = _to_money(getattr(plan, "price_rub", None))
    if rub > 0:
        return rub, "RUB"
    return _to_money(getattr(plan, "price_usd", None)), "USD"


def _validate_live_checkout_settings() -> None:
    if not settings.lava_api_key.strip():
        raise LavaCheckoutUnavailable("LAVA_API_KEY is required for live Lava checkout")
    if not settings.lava_offer_id.strip():
        raise LavaCheckoutUnavailable("LAVA_OFFER_ID is required for live Lava checkout")


def _periodicity_for_plan(plan: Plan) -> str:
    code = str(getattr(plan, "code", "") or "").lower()
    duration_days = int(getattr(plan, "duration_days", 0) or 0)
    if code in {"1m", "month", "monthly"} or 28 <= duration_days <= 31:
        return "MONTHLY"
    if code in {"6m", "half_year", "semiannual"} or 175 <= duration_days <= 186:
        return "PERIOD_180_DAYS"
    if code in {"12m", "year", "annual"} or 360 <= duration_days <= 370:
        return "PERIOD_YEAR"
    raise LavaCheckoutUnavailable(
        f"Unsupported Lava subscription period: code={code!r}, duration_days={duration_days}"
    )


async def _rub_offer_page_url(payment: Payment, periodicity: str, user: User) -> str:
    """Build the direct offer-page checkout URL for a RUB payment.

    Dead-end path, reachable only with ENABLE_LAVA_RUB_OFFER_PAGE=true (GK-418):
    payments completed on this page are never reported back to us. Kept as a
    rollback lever only.

    Fails closed on `_verified_offer` — the offer must publicly sell the plan's
    periodicity in RUB and at exactly the local plan price, otherwise the user
    would see a page total that differs from what the bot promised (GK-410
    guarantee). GK-482 moved that rule out of this function so the invoice path
    enforces the same one; two copies would be two chances for one of them to
    drift, on exactly the question this check exists to settle.
    """
    offer_id = settings.lava_offer_id.strip()
    product_id = await _verified_offer(payment, periodicity=periodicity, currency="RUB")
    query = urlencode(
        {
            "currency": "RUB",
            "language": _buyer_language(user).lower(),
            "utm_source": "telegram",
            "utm_medium": "bot",
            "utm_campaign": "membership_saas",
            # Lava returns clientUtm in webhooks; the webhook handler maps
            # utm_content=payment_{id} back to this local payment.
            "utm_content": f"payment_{payment.id}",
        }
    )
    return f"{_LAVA_PAGE_BASE_URL}/products/{product_id}/{offer_id}?{query}"


async def _verified_offer(payment: Payment, *, periodicity: str, currency: str) -> str:
    """Prove Lava sells this period at the price we quoted; return the product id.

    GK-482. Both checkout paths bill a fixed-price offer by naming `offerId` and
    `periodicity`, never an amount — so the figure that leaves the buyer's card
    is whatever Lava's catalogue says today, while the local `Payment` row, the
    revenue figures, the CRM export and the number the bot already printed to
    the member all come from `plan.price_rub` / `plan.price_usd`. Those two are
    equal only by convention. Reprice the offer on Lava's side, or drift a plan
    row here, and the member pays one number while every record we hold states
    another, with no symptom anywhere.

    The cost is one extra `/api/v2/products` call per checkout — same host, same
    API key, moments before the invoice call it guards. At this volume that is
    not worth caching, and a cache would reopen precisely the stale-price window
    this check exists to close.

    That cost is also a new hard dependency, and it is the reason for the second
    alert below: every RUB checkout now passes through `/api/v2/products`, so any
    hour Lava cannot serve that listing is an hour no member can buy in roubles —
    the audience's main method. Failing closed is right; failing closed quietly
    is what this function must not do.
    """
    offer_id = settings.lava_offer_id.strip()
    try:
        product_id, prices = await _fetch_offer_listing(offer_id)
    except LavaAPIError as exc:
        # Every reason `_fetch_offer_listing` raises — network, non-2xx, garbage
        # JSON, offer missing from the catalogue — lands here, and each one takes
        # RUB checkout offline until it clears. The bot answers with "use Stripe
        # or USDT" (`bot/handlers/subscription.py:709,723`) and pages nobody, so
        # without this the outage is visible only as roubles quietly stopping.
        logger.error(
            "lava offer lookup failed payment_id=%s periodicity=%s currency=%s error=%s",
            payment.id,
            periodicity,
            currency,
            exc,
        )
        await _alert_offer_lookup_failure(payment_id=payment.id, detail=str(exc))
        raise
    offer_amount = _offer_price(prices, periodicity=periodicity, currency=currency)
    quoted = _to_money(payment.amount)

    if offer_amount is None:
        logger.error(
            "lava offer no longer sells this period payment_id=%s periodicity=%s currency=%s",
            payment.id,
            periodicity,
            currency,
        )
        await _alert_offer_mismatch(
            payment_id=payment.id,
            detail=f"the offer no longer sells {periodicity} in {currency}",
            key=f"lava_offer_period_missing:{periodicity}:{currency}",
        )
        raise LavaCheckoutUnavailable(
            f"Lava Top offer does not sell this period in {currency}; use Stripe or USDT"
        )
    if offer_amount != quoted:
        logger.error(
            "lava offer price drift payment_id=%s periodicity=%s currency=%s "
            "offer_amount=%s quoted_amount=%s",
            payment.id,
            periodicity,
            currency,
            offer_amount,
            quoted,
        )
        await _alert_offer_mismatch(
            payment_id=payment.id,
            detail=(
                f"the offer charges {offer_amount} {currency} for {periodicity}, "
                f"the bot quoted {quoted}"
            ),
            key=f"lava_offer_price_drift:{periodicity}:{currency}",
        )
        raise LavaAPIError("Lava offer price does not match the selected local plan")
    return product_id


async def _alert_offer_mismatch(*, payment_id: int | None, detail: str, key: str) -> None:
    """Page a human, because refusing the checkout tells nobody why.

    Both exceptions `_verified_offer` raises are already caught in the bot and
    answered with "use Stripe or USDT" (`bot/handlers/subscription.py:709,723`)
    — a polite dead end that alerts no one. Without this, a repricing would take
    Lava checkout offline silently, which is the same class of failure as
    charging the wrong price and would be found the same way: by someone
    noticing the money stopped.

    Best-effort by construction: `send_ops_alert` returns False rather than
    raising, and the broad catch covers the import/config edges — an alert that
    could not be delivered must not turn a refused checkout into a crash.
    """
    await _best_effort_offer_alert(
        "GK-482: Lava refused a checkout — its offer disagrees with the price "
        "the bot quoted.\n"
        f"payment_id={payment_id}\n"
        f"{detail}\n"
        "Align Lava's offer first, then the local plan row — in that order.",
        key=key,
        what="lava offer mismatch",
    )


async def _alert_offer_lookup_failure(*, payment_id: int | None, detail: str) -> None:
    """The other way this guard refuses: the catalogue could not be read at all.

    Distinct from `_alert_offer_mismatch` because the answer is different. A
    mismatch means two numbers disagree and a human has to decide which is right.
    This means Lava told us nothing — an outage, a rotated key, a 503, or an offer
    id that no longer appears in the catalogue — and the checkout is refused not
    because the price is wrong but because it is unknown. Failing closed is
    correct (billing an unverified amount is the worse outcome), and it means RUB
    checkout is down for as long as the condition lasts.

    One bucket, not one per cause: during an outage every checkout raises, and
    the useful signal is "roubles are not selling right now", which is one alert
    an hour. `detail` carries which of the causes it was.
    """
    await _best_effort_offer_alert(
        "GK-482: Lava's product catalogue could not be read — RUB checkout is "
        "refusing every payment until it can.\n"
        f"payment_id={payment_id}\n"
        f"{detail}\n"
        "Members are being told to use Stripe or USDT. Check Lava's status and "
        "that LAVA_API_KEY and LAVA_OFFER_ID are still valid.",
        key="lava_offer_lookup_failed",
        what="lava offer lookup failure",
    )


async def _best_effort_offer_alert(body: str, *, key: str, what: str) -> None:
    """Send, swallow, log. Written once so both callers keep the same promise.

    Plain text on purpose — GK-451 escapes the whole body inside
    `send_ops_alert`, so markup here would arrive as literal characters.
    """
    try:
        await send_ops_alert(body, key=key, rate_limit_seconds=3600, severity="error")
    except Exception:  # noqa: BLE001 — alerting is not allowed to mask the refusal
        logger.warning("could not send ops alert for %s", what, exc_info=True)


async def _fetch_offer_listing(offer_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (product_id, offer prices) for the configured offer."""
    base_url = settings.lava_api_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
            response = await client.get(
                _LAVA_PRODUCTS_PATH,
                # Without this flag the listing returns only the offer's default
                # (monthly) price, hiding the 180-day/year prices that do exist —
                # which made 6m/12m RUB checkout fail closed as "period not sold".
                params={"showAllSubscriptionPeriods": "true"},
                headers={"X-Api-Key": settings.lava_api_key.strip()},
            )
    except httpx.RequestError as exc:
        raise LavaAPIError("Lava products request failed") from exc
    if not 200 <= response.status_code < 300:
        logger.warning("lava products rejected status=%s", response.status_code)
        raise LavaAPIError(f"Lava products returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise LavaAPIError("Lava products returned invalid JSON") from exc

    items = body.get("items") if isinstance(body, dict) else body
    if not isinstance(items, list):
        raise LavaAPIError("Lava products returned an invalid response object")
    for item in items:
        data = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), dict) else item
        if not isinstance(data, dict):
            continue
        product_id = str(data.get("id") or "")
        for offer in data.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            if str(offer.get("id") or "") == offer_id and product_id:
                prices = [p for p in offer.get("prices") or [] if isinstance(p, dict)]
                return product_id, prices
    raise LavaAPIError("Configured Lava offer was not found in the products list")


def _offer_price(
    prices: list[dict[str, Any]],
    *,
    periodicity: str,
    currency: str,
) -> Decimal | None:
    for price in prices:
        if (
            str(price.get("periodicity") or "").upper() == periodicity.upper()
            and str(price.get("currency") or "").upper() == currency.upper()
        ):
            return _to_money(price.get("amount"))
    return None


def _append_note(note: str | None, extra: str) -> str:
    return f"{note} | {extra}" if note else extra


def _buyer_language(user: User) -> str:
    language = str(getattr(user, "language", "") or "").lower()
    if language.startswith("ru"):
        return "RU"
    if language.startswith("es"):
        return "ES"
    return "EN"


async def _post_lava_invoice(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = settings.lava_api_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
            response = await client.post(
                _LAVA_CREATE_INVOICE_PATH,
                headers={"X-Api-Key": settings.lava_api_key.strip()},
                json=payload,
            )
    except httpx.RequestError as exc:
        raise LavaAPIError("Lava create-invoice request failed") from exc

    if not 200 <= response.status_code < 300:
        error = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = str(body.get("error") or "")
        except ValueError:
            pass
        logger.warning(
            "lava create invoice rejected status=%s error=%r",
            response.status_code,
            error[:200],
        )
        raise LavaAPIError(f"Lava create-invoice returned HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise LavaAPIError("Lava create-invoice returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise LavaAPIError("Lava create-invoice returned an invalid response object")
    return body


def _validate_invoice_total(
    invoice: dict[str, Any],
    *,
    expected_amount: object,
    expected_currency: str,
) -> None:
    total = _first_value(
        invoice,
        "amountTotal",
        "amount_total",
        "data.amountTotal",
        "data.amount_total",
    )
    if not isinstance(total, dict):
        # A response that echoes no total cannot be checked, and this used to
        # return in silence — the one shape in which this guard passes without
        # having verified anything. Left permissive on purpose: refusing here
        # would take live checkout down on an assumption about Lava's response
        # body, and since GK-482 the price was already proved against the
        # catalogue before the invoice was created. This is the second look.
        logger.info("lava create-invoice response carried no amountTotal to verify")
        return
    actual_amount = _to_money(total.get("amount"))
    actual_currency = str(total.get("currency") or "").upper()
    expected = _to_money(expected_amount)
    if actual_amount != expected or actual_currency != expected_currency.upper():
        raise LavaAPIError(
            "Lava create-invoice total does not match the selected local plan"
        )


def _payment_id_from_utm(payload: dict[str, Any]) -> str | None:
    content = _first_text(payload, "clientUtm.utm_content", "client_utm.utm_content") or ""
    prefix = "payment_"
    if not content.startswith(prefix):
        return None
    payment_id = content[len(prefix) :]
    return payment_id if payment_id.isdigit() else None


def _to_money(value: object) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _verify_x_api_key(x_api_key: str | None) -> bool:
    expected = settings.lava_webhook_api_key.strip()
    if not expected:
        logger.error("lava webhook rejected: LAVA_WEBHOOK_API_KEY is not configured")
        return False
    if not x_api_key:
        logger.warning("lava webhook missing X-Api-Key header")
        return False
    return hmac.compare_digest(expected, x_api_key.strip())


def _verify_basic_authorization(authorization: str | None) -> bool:
    expected_user = settings.lava_webhook_basic_username
    expected_password = settings.lava_webhook_basic_password
    if not expected_user or not expected_password:
        logger.error("lava webhook rejected: Basic auth credentials are not configured")
        return False
    if not authorization or not authorization.lower().startswith("basic "):
        logger.warning("lava webhook missing Basic Authorization header")
        return False
    encoded = authorization.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        logger.warning("lava webhook malformed Basic Authorization header")
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        logger.warning("lava webhook malformed Basic credentials")
        return False
    user_ok = hmac.compare_digest(username, expected_user)
    password_ok = hmac.compare_digest(password, expected_password)
    return bool(user_ok & password_ok)


def _infer_event_type(payload: dict[str, Any]) -> str:
    status = (
        _first_text(payload, "status", "contract.status", "payment.status", "subscription.status")
        or ""
    ).strip().lower()
    is_recurring = bool(
        _first_value(
            payload,
            "parentContractId",
            "parent_contract_id",
            "parentInvoiceId",
            "parent_invoice_id",
        )
    )
    if status in {"success", "succeeded", "paid"}:
        return "subscription.recurring.payment.success" if is_recurring else "payment.success"
    if status in {"failed", "declined", "error"}:
        return "subscription.recurring.payment.failed" if is_recurring else "payment.failed"
    if status in {"cancelled", "canceled"}:
        return "subscription.cancelled"
    return ""


def _normalize_event_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in _LAVA_EVENTS:
        return raw
    # Prefer the longest event token: "payment.failed" is a suffix of
    # "subscription.recurring.payment.failed" and must not win first.
    for known in sorted(_LAVA_EVENTS, key=len, reverse=True):
        if known in raw:
            return known
    return raw


def _first_text(payload: dict[str, Any], *paths: str) -> str | None:
    value = _first_value(payload, *paths)
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = _first_value(value, "id", "value", "amount")
        if value in (None, ""):
            return None
    return str(value)


def _first_decimal(payload: dict[str, Any], *paths: str) -> Decimal | None:
    value = _first_value(payload, *paths)
    if isinstance(value, dict):
        value = _first_value(value, "value", "amount", "sum")
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _first_value(payload: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = payload
        for key in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current
    return None


# ---------------------------------------------------------------------------
# GK-415 — the read side: what Lava thinks it has sold.
#
# The push half (webhooks) is GK-416. This is the pull half: a sale Lava
# completed that never reached us produces no webhook to alert on, so the only
# way to see it is to ask. Made possible by GK-418 — platform/offer-page sales
# are invisible to this API permanently (confirmed by Lava support and measured
# twice), while API-created invoices appear in `/api/v1/invoices` with the
# `clientUtm` that joins them back to a local payment id.
#
# Field access goes through `_first_value` throughout, because Lava's read
# shapes are not the ones documented for the write endpoints and have already
# been observed to differ between endpoints (`/sales` returns a per-product
# shape that `/invoices` does not). Every accessor lists the alternatives
# actually seen in the live probes rather than one assumed path.
# ---------------------------------------------------------------------------

_LAVA_INVOICES_PATH = "/api/v1/invoices"
#: Statuses Lava uses for "the buyer paid and it settled".
LAVA_COMPLETED_STATUSES = frozenset({"completed", "subscription-active"})
_LAVA_PAGE_SIZE = 100
#: Refuse to walk the journal forever if the API ignores our paging.
_LAVA_MAX_PAGES = 20


class RemoteSale:
    """One completed sale as Lava reports it, with our fields pulled out."""

    __slots__ = ("raw", "id", "status", "amount", "currency", "created_at", "payment_id", "buyer_email", "contract_id")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.id = _remote_str(raw, "id", "invoiceId", "invoice.id")
        self.status = (_remote_str(raw, "status", "invoiceStatus", "receipt.status") or "").lower()
        self.amount = _to_optional_money(
            _first_value(raw, "receipt.amount", "amountTotal.amount", "amount", "sum")
        )
        self.currency = (
            _remote_str(raw, "receipt.currency", "amountTotal.currency", "currency") or ""
        ).upper()
        self.created_at = _remote_dt(
            raw, "createdAt", "created", "receipt.createdAt", "timestamp"
        )
        self.payment_id = _remote_payment_id(raw)
        self.buyer_email = (_remote_str(raw, "buyer.email", "email", "clientEmail") or "").lower() or None
        self.contract_id = _remote_str(raw, "contractId", "subscriptionId", "parentContractId")

    @property
    def is_completed(self) -> bool:
        return self.status in LAVA_COMPLETED_STATUSES

    def describe(self) -> dict[str, Any]:
        """What a human needs in the alert to fulfil this by hand."""
        return {
            "remote_id": self.id,
            "status": self.status,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency or None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "buyer_email": self.buyer_email,
            "contract_id": self.contract_id,
            "utm_payment_id": self.payment_id,
        }


def _remote_str(payload: dict[str, Any], *paths: str) -> str | None:
    value = _first_value(payload, *paths)
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _to_optional_money(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = _first_value(value, "amount", "value", "sum")
        if value in (None, ""):
            return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _remote_dt(payload: dict[str, Any], *paths: str) -> datetime | None:
    raw = _first_value(payload, *paths)
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _remote_payment_id(raw: dict[str, Any]) -> int | None:
    """`clientUtm.utm_content = payment_94` is the correlation key.

    Proven on real money: the 06-07.07 invoices carried `payment_61/62/63` and
    the 29.07 one carried `payment_94`. Deliberately delegates to the webhook
    side's `_payment_id_from_utm` instead of restating the rule — two copies of
    a correlation key are two chances for one of them to drift into a silent
    mismatch, which is precisely the failure this whole check exists to catch.
    """
    content = _payment_id_from_utm(raw)
    return int(content) if content else None


class LavaSalesGateway:
    """Read-only view of the Lava journal. Never mutates anything."""

    async def list_recent_sales(self, *, since: datetime) -> list[RemoteSale]:
        base_url = settings.lava_api_base_url.rstrip("/")
        headers = {"X-Api-Key": settings.lava_api_key.strip()}
        sales: list[RemoteSale] = []
        seen: set[str] = set()

        async with httpx.AsyncClient(base_url=base_url, timeout=25.0) as client:
            for page in range(_LAVA_MAX_PAGES):
                try:
                    response = await client.get(
                        _LAVA_INVOICES_PATH,
                        headers=headers,
                        params={"size": _LAVA_PAGE_SIZE, "page": page},
                    )
                except httpx.RequestError as exc:
                    raise LavaAPIError("Lava invoices request failed") from exc
                if not 200 <= response.status_code < 300:
                    raise LavaAPIError(f"Lava invoices returned HTTP {response.status_code}")
                try:
                    body = response.json()
                except ValueError as exc:
                    raise LavaAPIError("Lava invoices returned invalid JSON") from exc

                items = body.get("items") if isinstance(body, dict) else body
                if not isinstance(items, list) or not items:
                    break

                exhausted = False
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sale = RemoteSale(item)
                    # Older than the window: the journal is newest-first, so the
                    # rest of this page and every later page is older still.
                    if sale.created_at is not None and sale.created_at < since:
                        exhausted = True
                        continue
                    key = sale.id or json.dumps(item, sort_keys=True)[:200]
                    if key in seen:
                        continue
                    seen.add(key)
                    sales.append(sale)
                if exhausted or len(items) < _LAVA_PAGE_SIZE:
                    break
        return sales
