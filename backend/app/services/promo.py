"""Promo-code validation, redemption, and checkout discount (GK-210).

A promo code is an admin-created coupon with its own discount, optional plan
scope, usage cap, and validity window. It is distinct from the per-user referral
discount (``app/services/referral.py``) but shares the same money rules:

- The discount is computed and applied to the ``Payment`` row at *checkout
  creation* (mirroring how the referral discount works). For Stripe the
  amount is also expressed as a one-time coupon so renewals stay full price;
  for Lava and manual (USDT/Zelle) the discounted amount is what the user pays.
- The redemption row + ``redeemed_count`` increment also happen at checkout
  creation. One redemption per (code, user) is enforced by a unique constraint,
  so a user cannot redeem the same code twice. A known, documented limitation:
  an abandoned checkout still consumes the user's single use of that code.
- Promo and referral discounts never stack. When a valid promo is supplied it
  replaces the referral discount; otherwise the referral discount path runs
  unchanged.
- An "influencer" code (``referrer_user_id`` set) additionally writes a
  first-touch ``ReferralAttribution`` (source=``promo_code``) via
  ``link_referral`` so the code owner earns commission through the existing
  ledger — this is the "promo redemption links to attribution" requirement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Payment,
    Plan,
    PromoCode,
    PromoRedemption,
    ReferralDiscountReservation,
    User,
    utcnow,
)
from app.services.referral import (
    REFERRAL_SOURCE_PROMO_CODE,
    link_referral,
    reserve_referral_discount,
)

logger = logging.getLogger(__name__)

_MONEY = Decimal("0.01")
_HUNDRED = Decimal("100")
_MAX_CODE_LEN = 32

DISCOUNT_PERCENT = "percent"
DISCOUNT_FIXED = "fixed"

# validate_promo_code status values
PROMO_VALID = "valid"
PROMO_NOT_FOUND = "not_found"
PROMO_INACTIVE = "inactive"
PROMO_NOT_YET_ACTIVE = "not_yet_active"
PROMO_EXPIRED = "expired"
PROMO_PLAN_NOT_ELIGIBLE = "plan_not_eligible"
PROMO_EXHAUSTED = "exhausted"
PROMO_ALREADY_REDEEMED = "already_redeemed"
PROMO_GIFT_NOT_ELIGIBLE = "gift_not_eligible"


@dataclass(frozen=True)
class PromoValidation:
    status: str
    promo: PromoCode | None = None
    final_amount: Decimal | None = None
    discount_amount: Decimal | None = None

    @property
    def valid(self) -> bool:
        return self.status == PROMO_VALID


@dataclass(frozen=True)
class CheckoutDiscount:
    """Unified result for the checkout discount across promo + referral."""

    amount: Decimal          # final amount to charge
    original_amount: Decimal
    applied: bool
    kind: str | None = None  # "promo" | "referral" | None
    code: str | None = None
    promo: PromoCode | None = None
    promo_redemption: PromoRedemption | None = None
    referral_reservation: ReferralDiscountReservation | None = None
    discount_amount: Decimal = field(default_factory=lambda: Decimal("0"))

    def link_payment(self, payment: Payment) -> None:
        """Point the just-created Payment at the recorded promo/referral row."""
        if payment is None:
            return
        if self.promo_redemption is not None:
            self.promo_redemption.payment_id = payment.id
        if self.referral_reservation is not None:
            self.referral_reservation.payment_id = payment.id


def normalize_code(code: str | None) -> str | None:
    """Trim, upper-case, and shape-check a promo code; None if unusable."""
    if not code:
        return None
    clean = code.strip().upper()
    if not clean or len(clean) > _MAX_CODE_LEN:
        return None
    # Codes are [A-Z0-9_-]; reject anything else before hitting the DB.
    if not all(ch.isalnum() or ch in "_-" for ch in clean):
        return None
    return clean


def compute_promo_discount(
    promo: PromoCode,
    base_amount: object,
    currency: str,
) -> tuple[Decimal, Decimal]:
    """Return (final_amount, discount_amount) for ``promo`` on ``base_amount``.

    Fixed-amount codes only discount a checkout denominated in the code's own
    currency; a currency mismatch yields no discount (documented limitation —
    we never auto-convert FX in the discount path).
    """
    base = _money(base_amount)
    if promo.discount_type == DISCOUNT_FIXED:
        if (promo.amount_off_currency or "USD").upper() != (currency or "USD").upper():
            return base, Decimal("0.00")
        off = _money(promo.amount_off or 0)
        if off < 0:
            off = Decimal("0.00")
        final = base - off
    else:  # percent (default)
        pct = Decimal(str(promo.percent_off or 0))
        if pct < 0:
            pct = Decimal("0")
        if pct > _HUNDRED:
            pct = _HUNDRED
        final = _money(base * (_HUNDRED - pct) / _HUNDRED)

    if final < 0:
        final = Decimal("0.00")
    if final > base:
        final = base
    final = _money(final)
    return final, _money(base - final)


async def validate_promo_code(
    session: AsyncSession,
    code: str | None,
    user: User,
    plan: Plan,
    *,
    base_amount: object,
    currency: str = "USD",
    is_gift: bool = False,
) -> PromoValidation:
    """Read-only check of a promo code for ``user`` buying ``plan``.

    Order is deterministic so the (read) DB hits are predictable: load → active
    → window → plan eligibility → usage cap → per-user duplicate.
    """
    if is_gift:
        return PromoValidation(status=PROMO_GIFT_NOT_ELIGIBLE)

    clean = normalize_code(code)
    if clean is None:
        return PromoValidation(status=PROMO_NOT_FOUND)

    promo = await _load_promo(session, clean)
    if promo is None:
        return PromoValidation(status=PROMO_NOT_FOUND)
    if not promo.is_active:
        return PromoValidation(status=PROMO_INACTIVE, promo=promo)

    now = utcnow()
    if promo.valid_from is not None and now < promo.valid_from:
        return PromoValidation(status=PROMO_NOT_YET_ACTIVE, promo=promo)
    if promo.valid_until is not None and now > promo.valid_until:
        return PromoValidation(status=PROMO_EXPIRED, promo=promo)

    if not _plan_eligible(promo, plan):
        return PromoValidation(status=PROMO_PLAN_NOT_ELIGIBLE, promo=promo)

    if promo.max_redemptions is not None and int(promo.redeemed_count or 0) >= int(
        promo.max_redemptions
    ):
        return PromoValidation(status=PROMO_EXHAUSTED, promo=promo)

    if await _user_already_redeemed(session, promo.id, user.id):
        return PromoValidation(status=PROMO_ALREADY_REDEEMED, promo=promo)

    final, discount = compute_promo_discount(promo, base_amount, currency)
    return PromoValidation(
        status=PROMO_VALID,
        promo=promo,
        final_amount=final,
        discount_amount=discount,
    )


async def record_promo_redemption(
    session: AsyncSession,
    promo: PromoCode,
    user: User,
    plan: Plan,
    *,
    payment: Payment | None,
    original_amount: object,
    final_amount: object,
    discount_amount: object,
    currency: str = "USD",
) -> PromoRedemption | None:
    """Atomically reserve a slot, persist a redemption, and link attribution.

    The caller is expected to have validated first (e.g. via
    ``discount_for_checkout``); this writes the side effects in one place.

    The usage cap is enforced with a single conditional UPDATE so it holds under
    concurrency: two distinct users cannot both consume the last slot. Returns
    ``None`` (nothing written) when the cap was reached between validation and
    reservation — the caller then falls back to the referral path.
    """
    reserved_count = (
        await session.execute(
            update(PromoCode)
            .where(
                PromoCode.id == promo.id,
                or_(
                    PromoCode.max_redemptions.is_(None),
                    PromoCode.redeemed_count < PromoCode.max_redemptions,
                ),
            )
            .values(redeemed_count=PromoCode.redeemed_count + 1)
            .returning(PromoCode.redeemed_count)
        )
    ).scalar_one_or_none()
    if reserved_count is None:
        return None

    redemption = PromoRedemption(
        promo_code_id=promo.id,
        user_id=user.id,
        payment_id=payment.id if payment is not None else None,
        status="applied",
        plan_code=getattr(plan, "code", None),
        currency=currency,
        original_amount=_money(original_amount),
        discount_amount=_money(discount_amount),
        final_amount=_money(final_amount),
    )
    session.add(redemption)
    await session.flush()
    # Reconcile the in-memory ORM object with the value reserved in the DB.
    promo.redeemed_count = int(reserved_count)

    # Influencer code: attribute the buyer to the code owner so the referral
    # ledger pays commission. Skip gift purchases and self-attribution.
    if promo.referrer_user_id and not getattr(payment, "is_gift", False):
        referrer = (
            await session.execute(select(User).where(User.id == promo.referrer_user_id))
        ).scalar_one_or_none()
        if referrer is not None and referrer.id != user.id:
            await link_referral(
                session,
                referrer,
                user,
                source=REFERRAL_SOURCE_PROMO_CODE,
                code=promo.code,
            )
    return redemption


async def discount_for_checkout(
    session: AsyncSession,
    user: User,
    plan: Plan,
    base_amount: object,
    currency: str = "USD",
    *,
    promo_code: str | None = None,
    is_gift: bool = False,
) -> CheckoutDiscount:
    """Resolve the effective checkout discount: promo first, else referral.

    When a valid promo applies, the redemption is recorded immediately (one use
    consumed). An invalid/exhausted/duplicate promo is ignored and the referral
    discount path runs unchanged, so an expired code never blocks a purchase.
    """
    base = _money(base_amount)

    if promo_code and not is_gift:
        validation = await validate_promo_code(
            session,
            promo_code,
            user,
            plan,
            base_amount=base,
            currency=currency,
            is_gift=is_gift,
        )
        if validation.valid and validation.promo is not None:
            final = validation.final_amount if validation.final_amount is not None else base
            discount_amount = (
                validation.discount_amount
                if validation.discount_amount is not None
                else (base - final)
            )
        else:
            final = base
            discount_amount = Decimal("0.00")

        # A valid promo that yields no actual discount in this currency (e.g. a
        # fixed code priced in a different currency) is skipped — never consume a
        # redemption for zero benefit; fall through to the referral path instead.
        if validation.valid and validation.promo is not None and discount_amount > 0:
            redemption = await record_promo_redemption(
                session,
                validation.promo,
                user,
                plan,
                payment=None,
                original_amount=base,
                final_amount=final,
                discount_amount=discount_amount,
                currency=currency,
            )
            if redemption is not None:
                return CheckoutDiscount(
                    amount=final,
                    original_amount=base,
                    applied=True,
                    kind="promo",
                    code=validation.promo.code,
                    promo=validation.promo,
                    promo_redemption=redemption,
                    discount_amount=discount_amount,
                )
            # The cap was reached between validation and the atomic reservation
            # (a concurrent buyer took the last slot). Fall through to the
            # referral path rather than failing the checkout.
            logger.info(
                "promo %r cap reached at reservation for user %s",
                promo_code,
                getattr(user, "id", None),
            )
        if not validation.valid:
            logger.info(
                "promo %r not applied for user %s: %s",
                promo_code,
                getattr(user, "id", None),
                validation.status,
            )

    reservation_result = await reserve_referral_discount(
        session, user, plan, base, currency=currency, is_gift=is_gift
    )
    referral = reservation_result.discount
    if referral.applied:
        return CheckoutDiscount(
            amount=_money(referral.amount),
            original_amount=base,
            applied=True,
            kind="referral",
            code=referral.code,
            referral_reservation=reservation_result.reservation,
            discount_amount=_money(base - _money(referral.amount)),
        )
    return CheckoutDiscount(
        amount=base,
        original_amount=base,
        applied=False,
        kind=None,
        code=None,
        discount_amount=Decimal("0.00"),
    )


def _plan_eligible(promo: PromoCode, plan: Plan) -> bool:
    codes = promo.applies_to_plan_codes or []
    if not codes:
        return True
    plan_code = (getattr(plan, "code", "") or "").lower()
    return plan_code in {str(c).lower() for c in codes}


async def _load_promo(session: AsyncSession, code: str) -> PromoCode | None:
    return (
        await session.execute(select(PromoCode).where(PromoCode.code == code))
    ).scalar_one_or_none()


async def _user_already_redeemed(
    session: AsyncSession, promo_code_id: int, user_id: int
) -> bool:
    existing = (
        await session.execute(
            select(PromoRedemption.id)
            .where(
                PromoRedemption.promo_code_id == promo_code_id,
                PromoRedemption.user_id == user_id,
                PromoRedemption.status == "applied",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return existing is not None


async def redeemed_count(session: AsyncSession, promo_code_id: int) -> int:
    """Live count of applied redemptions (admin stats; not the hot path)."""
    return int(
        (
            await session.execute(
                select(func.count(PromoRedemption.id)).where(
                    PromoRedemption.promo_code_id == promo_code_id,
                    PromoRedemption.status == "applied",
                )
            )
        ).scalar_one()
        or 0
    )


def _money(value: object) -> Decimal:
    return Decimal(str(value if value is not None else "0")).quantize(
        _MONEY, rounding=ROUND_HALF_UP
    )
