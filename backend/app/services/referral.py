from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Payment,
    Plan,
    Referral,
    ReferralAttribution,
    ReferralDiscountReservation,
    User,
    utcnow,
)
from app.services.referral_ledger import record_referral_commission_intent
from app.services.security import generate_referral_code

_MAX_CODE_GENERATION_ATTEMPTS = 10
REFERRAL_SOURCE_TELEGRAM = "telegram_deeplink"
REFERRAL_SOURCE_PROMO_CODE = "promo_code"
REFERRAL_SOURCE_ADMIN = "admin"
REFERRAL_SOURCE_LEGACY = "legacy"
REFERRAL_DISCOUNT_PERCENT = Decimal("20")
REFERRAL_DISCOUNT_CODE = "monthly_first_invoice_20"
_REFERRAL_DISCOUNT_FACTOR = (Decimal("100") - REFERRAL_DISCOUNT_PERCENT) / Decimal("100")
_MONEY = Decimal("0.01")
# An abandoned discounted checkout holds the user's single referral-discount slot
# until this TTL lapses; a later checkout may then reclaim and re-reserve it.
# Sized well beyond any provider's checkout-completion window.
_RESERVATION_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class ReferralLinkResult:
    status: str
    referrer: User | None = None
    attribution: ReferralAttribution | None = None

    @property
    def linked(self) -> bool:
        return self.status == "linked"


@dataclass(frozen=True)
class ReferralDiscount:
    amount: Decimal
    applied: bool
    code: str | None = None


@dataclass(frozen=True)
class ReferralReservationResult:
    """Outcome of an at-checkout referral-discount reservation attempt.

    ``reservation`` is the persisted ``active`` row when the discount was won, and
    ``None`` when the user was ineligible or already holds the single slot (in
    which case ``discount.applied`` is False and the caller charges full price).
    """

    discount: ReferralDiscount
    reservation: ReferralDiscountReservation | None = None

    @property
    def applied(self) -> bool:
        return self.discount.applied


async def ensure_unique_code(session: AsyncSession) -> str:
    # Bounded retry: 36^8 = 2.8e12 codes, collision is astronomically unlikely,
    # but never loop forever if something is wrong with the random source.
    for _ in range(_MAX_CODE_GENERATION_ATTEMPTS):
        code = generate_referral_code()
        q = select(User.id).where(User.referral_code == code)
        if (await session.execute(q)).scalar_one_or_none() is None:
            return code
    raise RuntimeError("failed to generate unique referral code after retries")


async def resolve_referrer(session: AsyncSession, code: str) -> User | None:
    # Code is generated as 8 chars [A-Z0-9]; reject anything not matching the
    # shape before hitting the DB. Protects against pathological LIKE input
    # (we use == here but defence in depth) and overlong inputs.
    if not code or not (1 <= len(code) <= 16) or not code.isalnum():
        return None
    q = select(User).where(User.referral_code == code)
    return (await session.execute(q)).scalar_one_or_none()


async def link_referral_code(
    session: AsyncSession,
    code: str,
    referee: User,
    *,
    source: str = REFERRAL_SOURCE_TELEGRAM,
) -> ReferralLinkResult:
    referrer = await resolve_referrer(session, code)
    if referrer is None:
        return ReferralLinkResult(status="not_found")
    return await link_referral(session, referrer, referee, source=source, code=code)


async def link_referral(
    session: AsyncSession,
    referrer: User,
    referee: User,
    *,
    source: str = REFERRAL_SOURCE_TELEGRAM,
    code: str | None = None,
) -> ReferralLinkResult:
    """Atomically set first-touch referral attribution if none exists.

    Using a single UPDATE ... WHERE referrer_id IS NULL avoids a TOCTOU race
    where two concurrent /start ref_X & /start ref_Y could both pass the
    "is None" check before writing.
    """
    if referrer.id == referee.id:
        return ReferralLinkResult(status="self_referral", referrer=referrer)

    clean_source = _clean_source(source)
    clean_code = _clean_code(code)
    existing = await _load_attribution(session, referee.id)
    if existing is not None:
        return _handle_existing_attribution(existing, referrer, clean_source, clean_code)

    if referee.referrer_id is not None:
        attribution = await _create_attribution(
            session,
            referrer_id=referee.referrer_id,
            referee_id=referee.id,
            source=REFERRAL_SOURCE_LEGACY,
            code=None,
        )
        if referee.referrer_id == referrer.id:
            return ReferralLinkResult(
                status="already_linked",
                referrer=referrer,
                attribution=attribution,
            )
        return _handle_existing_attribution(attribution, referrer, clean_source, clean_code)

    stmt = (
        update(User)
        .where(User.id == referee.id, User.referrer_id.is_(None))
        .values(referrer_id=referrer.id)
    )
    result = await session.execute(stmt)
    if not getattr(result, "rowcount", 0):
        existing = await _load_attribution(session, referee.id)
        if existing is not None:
            return _handle_existing_attribution(existing, referrer, clean_source, clean_code)
        current_referrer_id = await _current_referrer_id(session, referee.id)
        if current_referrer_id is None:
            return ReferralLinkResult(status="race_lost", referrer=referrer)
        referee.referrer_id = current_referrer_id
        attribution = await _create_attribution(
            session,
            referrer_id=current_referrer_id,
            referee_id=referee.id,
            source=REFERRAL_SOURCE_LEGACY,
            code=None,
        )
        if current_referrer_id == referrer.id:
            return ReferralLinkResult(
                status="already_linked",
                referrer=referrer,
                attribution=attribution,
            )
        return _handle_existing_attribution(attribution, referrer, clean_source, clean_code)

    # Keep the in-memory object consistent for the caller.
    referee.referrer_id = referrer.id
    attribution = await _create_attribution(
        session,
        referrer_id=referrer.id,
        referee_id=referee.id,
        source=clean_source,
        code=clean_code,
    )
    return ReferralLinkResult(status="linked", referrer=referrer, attribution=attribution)


async def referral_discount_for_checkout(
    session: AsyncSession,
    user: User,
    plan: Plan,
    amount: object,
    *,
    is_gift: bool = False,
) -> ReferralDiscount:
    checkout_amount = _money(amount)
    if not await is_referral_discount_eligible(session, user, plan=plan, is_gift=is_gift):
        return ReferralDiscount(amount=checkout_amount, applied=False)
    return ReferralDiscount(
        amount=_money(checkout_amount * _REFERRAL_DISCOUNT_FACTOR),
        applied=True,
        code=REFERRAL_DISCOUNT_CODE,
    )


async def is_referral_discount_eligible(
    session: AsyncSession,
    user: User,
    *,
    plan: Plan,
    is_gift: bool = False,
) -> bool:
    if is_gift or not is_monthly_plan(plan) or user.referrer_id is None:
        return False
    prior = await session.execute(
        select(Payment.id)
        .where(
            Payment.user_id == user.id,
            Payment.status == "succeeded",
            Payment.is_gift.is_(False),
        )
        .limit(1)
    )
    return prior.scalar_one_or_none() is None


async def reserve_referral_discount(
    session: AsyncSession,
    user: User,
    plan: Plan,
    amount: object,
    *,
    currency: str = "USD",
    is_gift: bool = False,
) -> ReferralReservationResult:
    """Eligibility check + atomic single-use reservation for a REAL checkout.

    Unlike ``referral_discount_for_checkout`` (read-only, used by price previews),
    this records a durable ``active`` reservation and must only be called when a
    Payment is actually being created. Returns ``applied=False`` at full price
    when the user is ineligible or already holds the single referral-discount
    slot, so the discount can settle at most once even across concurrent or
    abandoned pending checkouts.
    """
    checkout_amount = _money(amount)
    if not await is_referral_discount_eligible(session, user, plan=plan, is_gift=is_gift):
        return ReferralReservationResult(
            discount=ReferralDiscount(amount=checkout_amount, applied=False)
        )

    reservation = await _reserve_discount_slot(
        session, user, plan, checkout_amount, currency=currency
    )
    if reservation is None:
        return ReferralReservationResult(
            discount=ReferralDiscount(amount=checkout_amount, applied=False)
        )
    return ReferralReservationResult(
        discount=ReferralDiscount(
            amount=_money(reservation.final_amount),
            applied=True,
            code=REFERRAL_DISCOUNT_CODE,
        ),
        reservation=reservation,
    )


async def _reserve_discount_slot(
    session: AsyncSession,
    user: User,
    plan: Plan,
    original_amount: Decimal,
    *,
    currency: str = "USD",
) -> ReferralDiscountReservation | None:
    """Atomically claim the user's single referral-discount slot.

    Releases the user's own *expired* active reservation first (so an abandoned
    checkout never blocks a legitimate retry), then inserts a fresh ``active``
    row inside a SAVEPOINT. The partial unique index
    ``uq_referral_discount_active_user`` rejects a second live/spent reservation,
    so a concurrent duplicate raises ``IntegrityError`` and we return ``None``
    (the caller falls back to full price). Mirrors the GK-401 USDT tx-claim.
    """
    now = utcnow()
    await session.execute(
        update(ReferralDiscountReservation)
        .where(
            ReferralDiscountReservation.user_id == user.id,
            ReferralDiscountReservation.status == "active",
            ReferralDiscountReservation.expires_at.is_not(None),
            ReferralDiscountReservation.expires_at <= now,
        )
        .values(status="released", released_at=now)
    )

    original = _money(original_amount)
    final = _money(original * _REFERRAL_DISCOUNT_FACTOR)
    reservation = ReferralDiscountReservation(
        user_id=user.id,
        referrer_id=getattr(user, "referrer_id", None),
        status="active",
        plan_code=getattr(plan, "code", None),
        currency=currency,
        original_amount=original,
        discount_amount=_money(original - final),
        final_amount=final,
        expires_at=now + _RESERVATION_TTL,
    )
    session.add(reservation)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # Another concurrent checkout holds the active/consumed slot. Detach the
        # rejected row so it is not retried on the caller's next flush.
        session.expunge(reservation)
        return None
    return reservation


async def consume_referral_discount_reservation(
    session: AsyncSession,
    payment: Payment,
) -> ReferralDiscountReservation | None:
    """Mark the payment's ``active`` referral reservation ``consumed`` (GK-402).

    Called from ``fulfill_payment`` on a successful non-gift payment. Only an
    ``active`` reservation linked to this payment is consumed, so a stale payment
    whose slot was already reclaimed/released is a no-op — the benefit settles
    exactly once. Idempotent: a re-fulfilled payment finds the row already
    ``consumed`` (or short-circuits earlier on ``approved_at``).
    """
    reservation = (
        await session.execute(
            select(ReferralDiscountReservation).where(
                ReferralDiscountReservation.payment_id == payment.id,
                ReferralDiscountReservation.status == "active",
            )
        )
    ).scalar_one_or_none()
    if reservation is None:
        return None
    reservation.status = "consumed"
    reservation.consumed_at = utcnow()
    return reservation


def is_monthly_plan(plan: Plan) -> bool:
    code = (plan.code or "").lower()
    duration_days = int(plan.duration_days)
    return code in {"1m", "month", "monthly"} or 28 <= duration_days <= 31


async def grant_referral_bonus(
    session: AsyncSession,
    referee: User,
    first_payment: Payment,
) -> tuple[Referral, User] | None:
    """Legacy API name kept for callers, now backed by the money ledger."""
    commission = await record_referral_commission_intent(session, referee, first_payment)
    if commission is None:
        return None

    referrer = (
        await session.execute(select(User).where(User.id == referee.referrer_id))
    ).scalar_one_or_none()
    if referrer is None:
        return None

    ref = (
        await session.execute(select(Referral).where(Referral.id == commission.referral_id))
    ).scalar_one_or_none()
    if ref is None:
        return None
    return ref, referrer


async def leaderboard(session: AsyncSession, limit: int = 20) -> list[tuple[User, int, int]]:
    """Returns [(user, referrals_count, total_bonus_days)] sorted by count desc."""
    q = (
        select(
            User,
            func.count(Referral.id).label("ref_count"),
            func.coalesce(func.sum(Referral.bonus_days_granted), 0).label("bonus_total"),
        )
        .join(Referral, Referral.referrer_id == User.id)
        .group_by(User.id)
        .order_by(desc("ref_count"))
        .limit(limit)
    )
    res = await session.execute(q)
    return [(row[0], int(row[1]), int(row[2])) for row in res.all()]


async def my_referrals_count(session: AsyncSession, user_id: int) -> int:
    q = select(func.count(Referral.id)).where(Referral.referrer_id == user_id)
    return int((await session.execute(q)).scalar_one() or 0)


async def _load_attribution(
    session: AsyncSession,
    referee_id: int,
) -> ReferralAttribution | None:
    return (
        await session.execute(
            select(ReferralAttribution).where(ReferralAttribution.referee_id == referee_id)
        )
    ).scalar_one_or_none()


async def _current_referrer_id(session: AsyncSession, referee_id: int) -> int | None:
    return (
        await session.execute(select(User.referrer_id).where(User.id == referee_id))
    ).scalar_one_or_none()


async def _create_attribution(
    session: AsyncSession,
    *,
    referrer_id: int,
    referee_id: int,
    source: str,
    code: str | None,
) -> ReferralAttribution:
    attribution = ReferralAttribution(
        referrer_id=referrer_id,
        referee_id=referee_id,
        source=source,
        code=code,
        review_status="clear",
        ignored_attempt_count=0,
    )
    session.add(attribution)
    await session.flush()
    return attribution


def _handle_existing_attribution(
    attribution: ReferralAttribution,
    referrer: User,
    source: str,
    code: str | None,
) -> ReferralLinkResult:
    if attribution.referrer_id == referrer.id:
        return ReferralLinkResult(
            status="already_linked",
            referrer=referrer,
            attribution=attribution,
        )

    attribution.review_status = "suspicious"
    if not attribution.suspicious_reason:
        attribution.suspicious_reason = "multiple_referrers"
    attribution.ignored_attempt_count = int(attribution.ignored_attempt_count or 0) + 1
    attribution.last_ignored_referrer_id = referrer.id
    attribution.last_ignored_source = source
    attribution.last_ignored_code = code
    attribution.last_ignored_at = utcnow()
    return ReferralLinkResult(
        status="ignored_existing",
        referrer=referrer,
        attribution=attribution,
    )


def _clean_source(source: str) -> str:
    if source in {
        REFERRAL_SOURCE_TELEGRAM,
        REFERRAL_SOURCE_PROMO_CODE,
        REFERRAL_SOURCE_ADMIN,
        REFERRAL_SOURCE_LEGACY,
    }:
        return source
    return REFERRAL_SOURCE_TELEGRAM


def _clean_code(code: str | None) -> str | None:
    if code is None:
        return None
    clean = code.strip()
    if not clean:
        return None
    return clean[:64]


def _money(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(_MONEY, rounding=ROUND_HALF_UP)
