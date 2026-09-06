from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.db.models import ReferralAttribution
from app.services.referral import (
    REFERRAL_DISCOUNT_CODE,
    REFERRAL_SOURCE_PROMO_CODE,
    REFERRAL_SOURCE_TELEGRAM,
    link_referral,
    link_referral_code,
    referral_discount_for_checkout,
)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class RowCount:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.flushes = 0
        self.executes = 0
        self._next_id = 1000

    async def execute(self, _query):
        self.executes += 1
        value = self.results.pop(0)
        if isinstance(value, RowCount):
            return value
        return Result(value)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


def user(**overrides):
    data = {
        "id": 10,
        "tg_id": 10010,
        "username": "member",
        "referrer_id": None,
        "referral_code": "MEMBER10",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def plan(**overrides):
    data = {
        "id": 20,
        "code": "1m",
        "duration_days": 30,
        "price_usd": Decimal("19.00"),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_first_touch_attribution_sets_referrer_and_records_source():
    referrer = user(id=99, username="mentor", referral_code="ABC123")
    referee = user(id=10, referrer_id=None)
    session = FakeSession(None, RowCount(1))

    result = await link_referral(
        session,
        referrer,
        referee,
        source=REFERRAL_SOURCE_TELEGRAM,
        code="ABC123",
    )

    attribution = session.added[0]
    assert result.status == "linked"
    assert result.referrer is referrer
    assert result.attribution is attribution
    assert isinstance(attribution, ReferralAttribution)
    assert referee.referrer_id == referrer.id
    assert attribution.referrer_id == referrer.id
    assert attribution.referee_id == referee.id
    assert attribution.source == REFERRAL_SOURCE_TELEGRAM
    assert attribution.code == "ABC123"
    assert attribution.review_status == "clear"
    assert attribution.ignored_attempt_count == 0


@pytest.mark.asyncio
async def test_self_referral_is_rejected_without_db_write():
    member = user(id=10, referrer_id=None)
    session = FakeSession()

    result = await link_referral(session, member, member, code="MEMBER10")

    assert result.status == "self_referral"
    assert member.referrer_id is None
    assert session.executes == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_second_referrer_is_ignored_and_marked_for_review():
    existing = ReferralAttribution(
        id=77,
        referrer_id=99,
        referee_id=10,
        source=REFERRAL_SOURCE_TELEGRAM,
        code="FIRST99",
        review_status="clear",
        ignored_attempt_count=0,
    )
    new_referrer = user(id=88, username="other", referral_code="SECOND88")
    referee = user(id=10, referrer_id=99)
    session = FakeSession(existing)

    result = await link_referral(session, new_referrer, referee, code="SECOND88")

    assert result.status == "ignored_existing"
    assert referee.referrer_id == 99
    assert existing.referrer_id == 99
    assert existing.review_status == "suspicious"
    assert existing.suspicious_reason == "multiple_referrers"
    assert existing.ignored_attempt_count == 1
    assert existing.last_ignored_referrer_id == 88
    assert existing.last_ignored_source == REFERRAL_SOURCE_TELEGRAM
    assert existing.last_ignored_code == "SECOND88"
    assert existing.last_ignored_at is not None
    assert session.added == []


@pytest.mark.asyncio
async def test_promo_code_path_uses_same_attribution_ledger():
    referrer = user(id=99, username="promo_owner", referral_code="PROMO42")
    referee = user(id=10, referrer_id=None)
    session = FakeSession(referrer, None, RowCount(1))

    result = await link_referral_code(
        session,
        "PROMO42",
        referee,
        source=REFERRAL_SOURCE_PROMO_CODE,
    )

    attribution = session.added[0]
    assert result.status == "linked"
    assert referee.referrer_id == referrer.id
    assert attribution.referrer_id == referrer.id
    assert attribution.source == REFERRAL_SOURCE_PROMO_CODE
    assert attribution.code == "PROMO42"


@pytest.mark.asyncio
async def test_referral_discount_is_monthly_first_purchase_only():
    referred = user(id=10, referrer_id=99)
    first_month = await referral_discount_for_checkout(
        FakeSession(None),
        referred,
        plan(),
        Decimal("19.00"),
    )
    assert first_month.amount == Decimal("15.20")
    assert first_month.applied is True
    assert first_month.code == REFERRAL_DISCOUNT_CODE

    six_month = await referral_discount_for_checkout(
        FakeSession(),
        referred,
        plan(code="6m", duration_days=180, price_usd=Decimal("89.00")),
        Decimal("89.00"),
    )
    assert six_month.amount == Decimal("89.00")
    assert six_month.applied is False

    repeat_month = await referral_discount_for_checkout(
        FakeSession(123),
        referred,
        plan(),
        Decimal("19.00"),
    )
    assert repeat_month.amount == Decimal("19.00")
    assert repeat_month.applied is False
