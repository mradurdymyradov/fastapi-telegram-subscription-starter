from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import Update

from app.db.models import PromoRedemption, ReferralDiscountReservation, utcnow
from app.services.promo import (
    PROMO_ALREADY_REDEEMED,
    PROMO_EXHAUSTED,
    PROMO_EXPIRED,
    PROMO_GIFT_NOT_ELIGIBLE,
    PROMO_INACTIVE,
    PROMO_NOT_FOUND,
    PROMO_NOT_YET_ACTIVE,
    PROMO_PLAN_NOT_ELIGIBLE,
    PROMO_VALID,
    compute_promo_discount,
    discount_for_checkout,
    normalize_code,
    record_promo_redemption,
    validate_promo_code,
)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


class _NoopNested:
    """Async context manager standing in for ``session.begin_nested()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Returns the queued results in order; records added rows + flushes."""

    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.flushes = 0
        self.executes = 0
        self._next_id = 5000

    async def execute(self, _query):
        self.executes += 1
        value = self.results.pop(0)
        return Result(value)

    def add(self, obj):
        self.added.append(obj)

    def expunge(self, obj):
        if obj in self.added:
            self.added.remove(obj)

    def begin_nested(self):
        return _NoopNested()

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


class ReservingSession:
    """Session double that models the atomic conditional cap UPDATE in memory.

    Every ``UPDATE promo_codes ... WHERE ... redeemed_count < max_redemptions``
    is evaluated against the shared promo object, so two sequential calls behave
    like two racing connections: only reservations that still fit under the cap
    win and return the new count; the rest return ``None`` (0 rows updated).
    """

    def __init__(self, promo_obj, *select_results):
        self.promo = promo_obj
        self.select_results = list(select_results)
        self.added = []
        self.flushes = 0
        self._next_id = 6000

    async def execute(self, query):
        if isinstance(query, Update):
            cap = self.promo.max_redemptions
            if cap is None or int(self.promo.redeemed_count or 0) < int(cap):
                self.promo.redeemed_count = int(self.promo.redeemed_count or 0) + 1
                return Result(self.promo.redeemed_count)
            return Result(None)
        return Result(self.select_results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


def promo(**overrides):
    data = {
        "id": 700,
        "code": "WELCOME20",
        "discount_type": "percent",
        "percent_off": Decimal("20"),
        "amount_off": None,
        "amount_off_currency": "USD",
        "applies_to_plan_codes": [],
        "max_redemptions": None,
        "redeemed_count": 0,
        "valid_from": None,
        "valid_until": None,
        "is_active": True,
        "referrer_user_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def user(**overrides):
    data = {"id": 10, "tg_id": 10010, "username": "member", "referrer_id": None}
    data.update(overrides)
    return SimpleNamespace(**data)


def plan(**overrides):
    data = {"id": 20, "code": "1m", "duration_days": 30, "price_usd": Decimal("19.00")}
    data.update(overrides)
    return SimpleNamespace(**data)


# ── normalize_code ─────────────────────────────────────────────────────────
def test_normalize_code_trims_and_uppercases():
    assert normalize_code("  welcome20 ") == "WELCOME20"
    assert normalize_code("Save-10_x") == "SAVE-10_X"


def test_normalize_code_rejects_bad_shapes():
    assert normalize_code("") is None
    assert normalize_code(None) is None
    assert normalize_code("has space") is None
    assert normalize_code("emoji😀") is None
    assert normalize_code("x" * 33) is None


# ── compute_promo_discount ───────────────────────────────────────────────────
def test_compute_percent_discount():
    final, discount = compute_promo_discount(promo(percent_off=Decimal("20")), Decimal("19.00"), "USD")
    assert final == Decimal("15.20")
    assert discount == Decimal("3.80")


def test_compute_fixed_discount_same_currency():
    p = promo(discount_type="fixed", percent_off=None, amount_off=Decimal("5.00"))
    final, discount = compute_promo_discount(p, Decimal("19.00"), "USD")
    assert final == Decimal("14.00")
    assert discount == Decimal("5.00")


def test_compute_fixed_discount_currency_mismatch_is_noop():
    p = promo(discount_type="fixed", percent_off=None, amount_off=Decimal("500"), amount_off_currency="RUB")
    final, discount = compute_promo_discount(p, Decimal("19.00"), "USD")
    assert final == Decimal("19.00")
    assert discount == Decimal("0.00")


def test_compute_fixed_discount_clamps_to_zero():
    p = promo(discount_type="fixed", percent_off=None, amount_off=Decimal("99.00"))
    final, discount = compute_promo_discount(p, Decimal("19.00"), "USD")
    assert final == Decimal("0.00")
    assert discount == Decimal("19.00")


# ── validate_promo_code ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_validate_valid_code():
    session = FakeSession(promo(), None)  # load promo, no prior redemption
    result = await validate_promo_code(
        session, "welcome20", user(), plan(), base_amount=Decimal("19.00"), currency="USD"
    )
    assert result.status == PROMO_VALID
    assert result.valid is True
    assert result.final_amount == Decimal("15.20")
    assert result.discount_amount == Decimal("3.80")
    assert session.executes == 2


@pytest.mark.asyncio
async def test_validate_not_found():
    session = FakeSession(None)
    result = await validate_promo_code(
        session, "missing", user(), plan(), base_amount=Decimal("19.00")
    )
    assert result.status == PROMO_NOT_FOUND


@pytest.mark.asyncio
async def test_validate_inactive():
    session = FakeSession(promo(is_active=False))
    result = await validate_promo_code(
        session, "welcome20", user(), plan(), base_amount=Decimal("19.00")
    )
    assert result.status == PROMO_INACTIVE


@pytest.mark.asyncio
async def test_validate_not_yet_active():
    future = utcnow() + timedelta(days=1)
    session = FakeSession(promo(valid_from=future))
    result = await validate_promo_code(
        session, "welcome20", user(), plan(), base_amount=Decimal("19.00")
    )
    assert result.status == PROMO_NOT_YET_ACTIVE
    assert session.executes == 1  # returns before the duplicate-check query


@pytest.mark.asyncio
async def test_validate_expired():
    past = utcnow() - timedelta(days=1)
    session = FakeSession(promo(valid_until=past))
    result = await validate_promo_code(
        session, "welcome20", user(), plan(), base_amount=Decimal("19.00")
    )
    assert result.status == PROMO_EXPIRED
    assert session.executes == 1


@pytest.mark.asyncio
async def test_validate_plan_not_eligible():
    session = FakeSession(promo(applies_to_plan_codes=["6m", "12m"]))
    result = await validate_promo_code(
        session, "welcome20", user(), plan(code="1m"), base_amount=Decimal("19.00")
    )
    assert result.status == PROMO_PLAN_NOT_ELIGIBLE


@pytest.mark.asyncio
async def test_validate_exhausted():
    session = FakeSession(promo(max_redemptions=5, redeemed_count=5))
    result = await validate_promo_code(
        session, "welcome20", user(), plan(), base_amount=Decimal("19.00")
    )
    assert result.status == PROMO_EXHAUSTED
    assert session.executes == 1  # exhaustion is checked before the duplicate query


@pytest.mark.asyncio
async def test_validate_duplicate_redemption():
    session = FakeSession(promo(), 99)  # load promo, existing redemption id=99
    result = await validate_promo_code(
        session, "welcome20", user(), plan(), base_amount=Decimal("19.00")
    )
    assert result.status == PROMO_ALREADY_REDEEMED
    assert session.executes == 2


@pytest.mark.asyncio
async def test_validate_gift_not_eligible_short_circuits():
    session = FakeSession()  # no DB hit at all
    result = await validate_promo_code(
        session, "welcome20", user(), plan(), base_amount=Decimal("19.00"), is_gift=True
    )
    assert result.status == PROMO_GIFT_NOT_ELIGIBLE
    assert session.executes == 0


# ── discount_for_checkout ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_discount_for_checkout_applies_promo_and_records_redemption():
    p = promo()
    # validate: load promo + no prior redemption; reservation UPDATE → count 1.
    session = FakeSession(p, None, 1)
    result = await discount_for_checkout(
        session, user(), plan(), Decimal("19.00"), "USD", promo_code="welcome20"
    )
    assert result.applied is True
    assert result.kind == "promo"
    assert result.code == "WELCOME20"
    assert result.amount == Decimal("15.20")
    assert result.discount_amount == Decimal("3.80")
    # A redemption row was created and the running count bumped.
    redemption = session.added[0]
    assert isinstance(redemption, PromoRedemption)
    assert redemption.status == "applied"
    assert redemption.final_amount == Decimal("15.20")
    assert p.redeemed_count == 1
    assert result.promo_redemption is redemption


@pytest.mark.asyncio
async def test_discount_for_checkout_links_payment_to_redemption():
    p = promo()
    session = FakeSession(p, None, 1)  # validate (2) + reservation → count 1
    result = await discount_for_checkout(
        session, user(), plan(), Decimal("19.00"), "USD", promo_code="welcome20"
    )
    payment = SimpleNamespace(id=4242)
    result.link_payment(payment)
    assert result.promo_redemption.payment_id == 4242


@pytest.mark.asyncio
async def test_discount_for_checkout_no_promo_falls_back_to_full_price():
    session = FakeSession()  # referrer_id None ⇒ referral path needs no DB
    result = await discount_for_checkout(session, user(referrer_id=None), plan(), Decimal("19.00"), "USD")
    assert result.applied is False
    assert result.kind is None
    assert result.amount == Decimal("19.00")


@pytest.mark.asyncio
async def test_discount_for_checkout_expired_promo_falls_back_to_referral():
    past = utcnow() - timedelta(days=1)
    # validate: load expired promo (1 query, returns before duplicate check);
    # then referral path: prior-payment lookup None ⇒ eligible, reclaim UPDATE.
    session = FakeSession(promo(valid_until=past), None, None)
    result = await discount_for_checkout(
        session, user(referrer_id=99), plan(), Decimal("19.00"), "USD", promo_code="welcome20"
    )
    assert result.applied is True
    assert result.kind == "referral"
    assert result.amount == Decimal("15.20")
    # The expired promo recorded no redemption, but the referral path now reserves
    # the user's single one-time discount (GK-402).
    assert len(session.added) == 1
    reservation = session.added[0]
    assert isinstance(reservation, ReferralDiscountReservation)
    assert reservation.status == "active"
    assert result.referral_reservation is reservation


@pytest.mark.asyncio
async def test_discount_for_checkout_gift_skips_promo():
    session = FakeSession()  # gift short-circuits before any promo DB hit
    result = await discount_for_checkout(
        session,
        user(referrer_id=None),
        plan(),
        Decimal("19.00"),
        "USD",
        promo_code="welcome20",
        is_gift=True,
    )
    assert result.applied is False
    assert result.amount == Decimal("19.00")
    assert session.added == []


# ── atomic cap reservation (GK-403) ──────────────────────────────────────────
def _redeem_kwargs():
    return {
        "payment": None,
        "original_amount": Decimal("19.00"),
        "final_amount": Decimal("15.20"),
        "discount_amount": Decimal("3.80"),
        "currency": "USD",
    }


@pytest.mark.asyncio
async def test_record_redemption_returns_none_when_reservation_loses():
    # The conditional cap UPDATE reserves 0 rows (cap already reached) ⇒ nothing
    # is written and the redemption is reported as not applied.
    session = FakeSession(None)  # reservation UPDATE → 0 rows
    result = await record_promo_redemption(
        session, promo(max_redemptions=1, redeemed_count=1), user(), plan(), **_redeem_kwargs()
    )
    assert result is None
    assert session.added == []
    assert session.flushes == 0


@pytest.mark.asyncio
async def test_record_redemption_reserves_then_writes_row():
    session = FakeSession(3)  # reservation UPDATE → new redeemed_count 3
    p = promo(max_redemptions=5, redeemed_count=2)
    result = await record_promo_redemption(session, p, user(), plan(), **_redeem_kwargs())
    assert isinstance(result, PromoRedemption)
    assert session.added == [result]
    assert p.redeemed_count == 3  # in-memory ORM object reconciled to reserved value


@pytest.mark.asyncio
async def test_promo_cap_reservation_allows_exactly_cap_winners():
    # Two distinct users race for a cap-1 code. The atomic reservation lets only
    # the first through; the loser writes nothing and the count reconciles.
    p = promo(max_redemptions=1, redeemed_count=0)
    session = ReservingSession(p)
    first = await record_promo_redemption(session, p, user(id=1), plan(), **_redeem_kwargs())
    second = await record_promo_redemption(session, p, user(id=2), plan(), **_redeem_kwargs())
    assert first is not None
    assert second is None
    assert p.redeemed_count == 1  # cap not exceeded
    assert len(session.added) == 1  # only the winner has a redemption row


@pytest.mark.asyncio
async def test_discount_for_checkout_cap_lost_falls_back_to_referral():
    # Promo passes the unlocked read but loses the atomic reservation to a
    # concurrent buyer ⇒ fall through to the referral discount, record nothing.
    # validate: load promo + no prior redemption; promo reservation: 0 rows;
    # referral eligibility: prior-payment lookup returns None ⇒ eligible;
    # referral reservation (GK-402): release-expired UPDATE, result unused.
    session = FakeSession(promo(), None, None, None, None)
    result = await discount_for_checkout(
        session, user(referrer_id=99), plan(), Decimal("19.00"), "USD", promo_code="welcome20"
    )
    assert result.applied is True
    assert result.kind == "referral"
    assert result.amount == Decimal("15.20")
    # No promo redemption is written when the promo reservation loses; the
    # referral fallback (GK-402) reserves its own single-use discount row.
    assert not any(isinstance(o, PromoRedemption) for o in session.added)
    assert any(isinstance(o, ReferralDiscountReservation) for o in session.added)


@pytest.mark.asyncio
async def test_discount_for_checkout_cap_lost_no_referrer_falls_back_to_full_price():
    session = FakeSession(promo(), None, None)  # validate (2) + reservation 0 rows (1)
    result = await discount_for_checkout(
        session, user(referrer_id=None), plan(), Decimal("19.00"), "USD", promo_code="welcome20"
    )
    assert result.applied is False
    assert result.kind is None
    assert result.amount == Decimal("19.00")
    assert session.added == []
