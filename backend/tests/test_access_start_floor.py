"""GK-439: paid time starts on a fixed date, not on the day of payment.

Grant, 10.08: «человек платит в августе, а его оплаченный месяц засчитывается
с 1 сентября, не со дня оплаты». Before this, `create_or_extend_subscription`
started a first period at `now`, so a payment on 19.08 bought 19.08 → 19.09:
eleven days of a paid month spent before paid access even opened, and a
different renewal day for every member.

The mechanism is one setting. These tests pin the three things that make it
safe to ship two weeks before launch: it moves a *first* period, it never
touches a renewal, and with the setting empty nothing changes at all.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import Settings, parse_access_start_floor
from app.services import subscription as subscription_service
from app.services.subscription import access_start_floor, create_or_extend_subscription

FLOOR = datetime(2026, 9, 1, tzinfo=UTC)
#: A payment made during the launch window — after payments open (~19.08) and
#: before paid access starts (01.09). This is the case the task exists for.
AUGUST_PAYMENT = datetime(2026, 8, 19, 14, 30, tzinfo=UTC)


class ScalarsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """Just enough session for `create_or_extend_subscription`."""

    def __init__(self, *existing):
        self.existing = list(existing)
        self.added = []

    async def execute(self, _query):
        return ScalarsResult(self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 1


def make_plan(duration_days: int = 30):
    return SimpleNamespace(id=2, code="1m", name="1 month", duration_days=duration_days)


def make_user():
    return SimpleNamespace(id=10, tg_id=10010)


def make_active_subscription(expires_at: datetime):
    """An existing, access-holding Stripe subscription (i.e. a renewal case)."""
    return SimpleNamespace(
        id=7,
        user_id=10,
        plan_id=2,
        status="active",
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_live_1",
        provider_status="active",
        started_at=expires_at - timedelta(days=30),
        expires_at=expires_at,
        current_period_start=expires_at - timedelta(days=30),
        current_period_end=expires_at,
        cancel_at_period_end=False,
        access_revoked_at=None,
        grace_started_at=None,
        grace_ends_at=None,
        access_revoke_retry_after_at=None,
        access_revoke_error=None,
        invite_link=None,
        notified_expiring=False,
    )


@pytest.fixture
def floor_at(monkeypatch):
    """Set (or clear) the launch floor and freeze the clock `now` reads."""

    def _apply(floor: datetime | None, *, now: datetime = AUGUST_PAYMENT):
        monkeypatch.setattr(
            subscription_service, "settings", SimpleNamespace(access_start_floor_at=floor)
        )
        monkeypatch.setattr(subscription_service, "utcnow", lambda: now)
        return now

    return _apply


# ---------------------------------------------------------------------------
# the requirement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_august_payment_buys_september_whole(floor_at):
    floor_at(FLOOR)
    session = FakeSession()

    sub = await create_or_extend_subscription(
        session, user=make_user(), plan=make_plan(), source="stripe", provider="stripe"
    )

    assert sub.started_at == FLOOR
    assert sub.expires_at == FLOOR + timedelta(days=30)
    # The access predicate reads `current_period_end` for provider-backed rows,
    # so a floor that moved only `expires_at` would move nothing that matters.
    assert sub.current_period_start == FLOOR
    assert sub.current_period_end == FLOOR + timedelta(days=30)


@pytest.mark.asyncio
async def test_provider_billing_dates_cannot_shorten_a_floored_first_period(floor_at):
    """The Stripe prepaid-plus-trial shape sends a line whose period is the
    *purchase*, not the access window — start and end both at checkout. Taking
    it at face value would end the member's access on the day it began."""
    now = floor_at(FLOOR)
    session = FakeSession()

    sub = await create_or_extend_subscription(
        session,
        user=make_user(),
        plan=make_plan(),
        source="stripe",
        provider="stripe",
        current_period_start=now,
        current_period_end=now,
    )

    assert sub.started_at == FLOOR
    assert sub.expires_at == FLOOR + timedelta(days=30)


@pytest.mark.asyncio
async def test_a_payment_after_the_floor_behaves_exactly_as_today(floor_at):
    september = datetime(2026, 9, 20, tzinfo=UTC)
    floor_at(FLOOR, now=september)
    session = FakeSession()

    sub = await create_or_extend_subscription(
        session, user=make_user(), plan=make_plan(), source="stripe", provider="stripe"
    )

    assert sub.started_at == september
    assert sub.expires_at == september + timedelta(days=30)


@pytest.mark.asyncio
async def test_with_the_floor_unset_nothing_changes(floor_at):
    now = floor_at(None)
    session = FakeSession()

    sub = await create_or_extend_subscription(
        session, user=make_user(), plan=make_plan(), source="stripe", provider="stripe"
    )

    assert sub.started_at == now
    assert sub.expires_at == now + timedelta(days=30)


@pytest.mark.asyncio
async def test_a_renewal_chains_from_expires_at_and_never_rewinds_to_the_floor(floor_at):
    """The floor is a *first*-period rule. A member who already has access must
    keep every day of it — rewinding to the floor would delete paid time."""
    floor_at(FLOOR)
    existing_expiry = datetime(2026, 12, 1, tzinfo=UTC)
    session = FakeSession(make_active_subscription(existing_expiry))

    sub = await create_or_extend_subscription(
        session, user=make_user(), plan=make_plan(), source="stripe", provider="stripe"
    )

    assert sub.id == 7, "the existing row is extended, not replaced"
    assert sub.expires_at == existing_expiry + timedelta(days=30)


@pytest.mark.asyncio
async def test_the_floor_does_not_shorten_a_subscription_that_outlives_it(floor_at):
    """An active subscription expiring *before* the floor still chains from its
    own end date. `max(now, floor)` applies to the start of a first period, not
    to the end of one somebody already paid for."""
    floor_at(FLOOR)
    existing_expiry = datetime(2026, 8, 25, tzinfo=UTC)  # before the floor
    session = FakeSession(make_active_subscription(existing_expiry))

    sub = await create_or_extend_subscription(
        session, user=make_user(), plan=make_plan(), source="stripe", provider="stripe"
    )

    assert sub.expires_at == existing_expiry + timedelta(days=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_days", [30, 180, 365])
async def test_every_plan_length_starts_at_the_floor(floor_at, duration_days):
    """Grant's «1 октября» is the monthly case of a general rule: the first
    period starts at the floor and runs the plan's own length."""
    floor_at(FLOOR)
    session = FakeSession()

    sub = await create_or_extend_subscription(
        session,
        user=make_user(),
        plan=make_plan(duration_days=duration_days),
        source="stripe",
        provider="stripe",
    )

    assert sub.started_at == FLOOR
    assert sub.expires_at == FLOOR + timedelta(days=duration_days)


# ---------------------------------------------------------------------------
# the helper both clocks read
# ---------------------------------------------------------------------------


def test_a_floor_in_the_past_is_the_same_as_no_floor(floor_at):
    floor_at(FLOOR, now=datetime(2026, 10, 5, tzinfo=UTC))

    assert access_start_floor() is None


def test_a_floor_in_the_future_is_returned_as_utc(floor_at):
    floor_at(FLOOR)

    assert access_start_floor() == FLOOR


# ---------------------------------------------------------------------------
# the setting itself — a typo here would be silent and expensive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("2026-09-01T00:00:00Z", FLOOR),
        ("2026-09-01T00:00:00z", FLOOR),
        ("2026-09-01T00:00:00+00:00", FLOOR),
        ("2026-09-01", FLOOR),  # naive is read as UTC
        ("2026-09-01T03:00:00+03:00", FLOOR),  # normalised, not truncated
    ],
)
def test_the_floor_is_parsed_as_an_instant_in_utc(raw, expected):
    assert parse_access_start_floor(raw) == expected


def test_an_unparseable_floor_refuses_to_boot_rather_than_reading_as_off():
    """A shrug to `None` here means paid time silently starts on the day of
    payment — the exact defect this setting exists to prevent, arriving with no
    log line. `validate_security` is the startup gate."""
    settings = Settings(_env_file=None, access_start_floor="1 сентября")

    assert settings.access_start_floor_at is None  # never raises on the pay path
    assert any("ACCESS_START_FLOOR" in error for error in settings.validate_security())


def test_a_valid_floor_is_not_a_security_finding():
    settings = Settings(_env_file=None, access_start_floor="2026-09-01T00:00:00Z")

    assert not any("ACCESS_START_FLOOR" in error for error in settings.validate_security())
