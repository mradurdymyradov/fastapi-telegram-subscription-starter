"""GK-402: one-time referral-discount reservation.

The real single-use guarantee is the Postgres partial unique index
``uq_referral_discount_active_user`` (UNIQUE(user_id) WHERE status IN
('active','consumed')). The suite runs fully mocked, so ``ReservingSession``
models that index the way Postgres serialises concurrent inserts — the first
flush of a user's slot wins, every later flush raises ``IntegrityError`` — and
verifies the reservation *contract*: eligible + free slot → reserve & discount;
slot taken → full price; expired slot → reclaim & re-reserve; consume settles the
benefit exactly once.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Update

from app.db.models import ReferralDiscountReservation
from app.services.referral import (
    REFERRAL_DISCOUNT_CODE,
    consume_referral_discount_reservation,
    reserve_referral_discount,
)


class _NoopNested:
    """Async context manager standing in for ``session.begin_nested()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class ReservingSession:
    """In-memory model of ``referral_discount_reservations`` + its partial index.

    ``rows`` is shared across sessions to emulate distinct concurrent checkouts
    hitting one DB. ``flush`` enforces UNIQUE(user_id) WHERE status IN
    ('active','consumed'); the reclaim UPDATE releases the user's expired active
    rows; the eligibility SELECT returns ``prior_payment``.
    """

    def __init__(self, rows, *, prior_payment=None):
        self.rows = rows
        self.prior_payment = prior_payment
        self.pending = []
        self._next_id = 9000

    async def execute(self, statement):
        if isinstance(statement, Update):
            now = datetime.now(UTC)
            for row in self.rows:
                if (
                    row.status == "active"
                    and row.expires_at is not None
                    and row.expires_at <= now
                ):
                    row.status = "released"
                    row.released_at = now
            return _Scalar(None)
        # The only SELECT reserve() issues is the eligibility prior-payment query.
        return _Scalar(self.prior_payment)

    def add(self, obj):
        self.pending.append(obj)

    def expunge(self, obj):
        if obj in self.pending:
            self.pending.remove(obj)

    def begin_nested(self):
        return _NoopNested()

    async def flush(self):
        for row in list(self.pending):
            if row.status in ("active", "consumed") and any(
                other is not row
                and other.user_id == row.user_id
                and other.status in ("active", "consumed")
                for other in self.rows
            ):
                raise IntegrityError(
                    "duplicate referral reservation",
                    {},
                    Exception("uq_referral_discount_active_user"),
                )
            if getattr(row, "id", None) is None:
                row.id = self._next_id
                self._next_id += 1
            if row not in self.rows:
                self.rows.append(row)
        self.pending = []


class ConsumeSession:
    """Returns the single reservation the ``status == 'active'`` query would find
    (or ``None`` when the row is released/absent), for consume-path tests."""

    def __init__(self, reservation):
        self._reservation = reservation

    async def execute(self, _statement):
        return _Scalar(self._reservation)


def user(**overrides):
    data = {"id": 10, "tg_id": 10010, "username": "member", "referrer_id": 99}
    data.update(overrides)
    return SimpleNamespace(**data)


def plan(**overrides):
    data = {"id": 20, "code": "1m", "duration_days": 30, "price_usd": Decimal("19.00")}
    data.update(overrides)
    return SimpleNamespace(**data)


def active_reservation(**overrides):
    data = {
        "id": 7000,
        "user_id": 10,
        "referrer_id": 99,
        "payment_id": None,
        "status": "active",
        "plan_code": "1m",
        "currency": "USD",
        "original_amount": Decimal("19.00"),
        "discount_amount": Decimal("3.80"),
        "final_amount": Decimal("15.20"),
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
        "released_at": None,
        "consumed_at": None,
    }
    data.update(overrides)
    return ReferralDiscountReservation(**data)


@pytest.mark.asyncio
async def test_reserve_grants_discount_and_records_active_reservation():
    rows = []
    session = ReservingSession(rows, prior_payment=None)

    result = await reserve_referral_discount(session, user(), plan(), Decimal("19.00"))

    assert result.applied is True
    assert result.discount.amount == Decimal("15.20")
    assert result.discount.code == REFERRAL_DISCOUNT_CODE
    assert result.reservation is not None
    assert len(rows) == 1
    reservation = rows[0]
    assert reservation.status == "active"
    assert reservation.user_id == 10
    assert reservation.referrer_id == 99
    assert reservation.original_amount == Decimal("19.00")
    assert reservation.discount_amount == Decimal("3.80")
    assert reservation.final_amount == Decimal("15.20")
    assert reservation.expires_at is not None


@pytest.mark.asyncio
async def test_second_pending_checkout_gets_no_discount():
    # AC1: a live (non-expired) reservation already holds the user's slot.
    rows = [active_reservation()]
    session = ReservingSession(rows, prior_payment=None)

    result = await reserve_referral_discount(session, user(), plan(), Decimal("19.00"))

    assert result.applied is False
    assert result.discount.amount == Decimal("19.00")  # full price
    assert result.reservation is None
    # No second active/consumed row was persisted.
    assert sum(1 for r in rows if r.status in ("active", "consumed")) == 1


@pytest.mark.asyncio
async def test_expired_reservation_is_reclaimed_and_rereserved():
    # AC2: an expired abandoned reservation can be released and re-reserved.
    stale = active_reservation(
        id=6000, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    rows = [stale]
    session = ReservingSession(rows, prior_payment=None)

    result = await reserve_referral_discount(session, user(), plan(), Decimal("19.00"))

    assert result.applied is True
    assert stale.status == "released"
    assert stale.released_at is not None
    fresh = [r for r in rows if r.status == "active"]
    assert len(fresh) == 1
    assert fresh[0] is not stale


@pytest.mark.asyncio
async def test_referrerless_user_is_not_reserved():
    rows = []
    session = ReservingSession(rows, prior_payment=None)

    result = await reserve_referral_discount(
        session, user(referrer_id=None), plan(), Decimal("19.00")
    )

    assert result.applied is False
    assert result.reservation is None
    assert rows == []


@pytest.mark.asyncio
async def test_prior_succeeded_payment_blocks_reservation():
    rows = []
    session = ReservingSession(rows, prior_payment=123)  # a prior succeeded payment

    result = await reserve_referral_discount(session, user(), plan(), Decimal("19.00"))

    assert result.applied is False
    assert result.reservation is None
    assert rows == []


@pytest.mark.asyncio
async def test_non_monthly_plan_is_not_reserved():
    rows = []
    session = ReservingSession(rows, prior_payment=None)

    result = await reserve_referral_discount(
        session,
        user(),
        plan(code="6m", duration_days=180, price_usd=Decimal("79.00")),
        Decimal("79.00"),
    )

    assert result.applied is False
    assert rows == []


@pytest.mark.asyncio
async def test_gift_checkout_is_not_reserved():
    rows = []
    session = ReservingSession(rows, prior_payment=None)

    result = await reserve_referral_discount(
        session, user(), plan(), Decimal("19.00"), is_gift=True
    )

    assert result.applied is False
    assert rows == []


@pytest.mark.asyncio
async def test_concurrent_reservations_grant_exactly_one():
    # AC1: N concurrent checkouts for one user → exactly one discounted, one row.
    rows = []
    sessions = [ReservingSession(rows, prior_payment=None) for _ in range(20)]

    results = await asyncio.gather(
        *[
            reserve_referral_discount(s, user(), plan(), Decimal("19.00"))
            for s in sessions
        ]
    )

    winners = [r for r in results if r.applied]
    losers = [r for r in results if not r.applied]
    assert len(winners) == 1
    assert len(losers) == 19
    assert sum(1 for r in rows if r.status in ("active", "consumed")) == 1
    assert all(loser.discount.amount == Decimal("19.00") for loser in losers)


@pytest.mark.asyncio
async def test_consume_marks_active_reservation_consumed():
    reservation = active_reservation(payment_id=555)
    session = ConsumeSession(reservation)

    consumed = await consume_referral_discount_reservation(
        session, SimpleNamespace(id=555)
    )

    assert consumed is reservation
    assert reservation.status == "consumed"
    assert reservation.consumed_at is not None


@pytest.mark.asyncio
async def test_consume_is_noop_when_no_active_reservation():
    # AC3: a stale/reclaimed (released) reservation is excluded by the
    # status == 'active' filter, so the query returns None and the benefit is
    # not spent a second time.
    session = ConsumeSession(None)

    consumed = await consume_referral_discount_reservation(
        session, SimpleNamespace(id=555)
    )

    assert consumed is None
