"""GK-434.2: a subscription's billing period may not run backwards.

`sub#6` on the live database carries `current_period_start = 2027-01-20` and
`current_period_end = 2026-07-28`. The row is cancelled, so nothing depends on
it today — but some write produced it, and date arithmetic on such a row gives
nonsense. No invariant existed anywhere, and three places set the two fields
independently, so the defect belongs to their combination rather than to any
one of them:

  1. `create_or_extend_subscription`'s renewal branch writes the *end* of the
     window in progress into `current_period_start` when the provider supplied
     no period of its own;
  2. `_handle_stripe_subscription_event` and
  3. `_handle_lava_subscription_event` each write the two fields under separate
     `if … is not None` guards, so a payload carrying one and not the other
     moves one end of the interval and leaves the other where it was.

These pin the guard, and both webhook shapes that can reach it.

No database is available in this image, and `before_insert` / `before_update`
fire during flush. So the wiring and the behaviour are asserted separately: one
test proves the listener really is registered against the mapper (which is what
makes it unbypassable by a fourth writer), and the rest drive the real handlers
against a real `Subscription` and then invoke the listener the way a flush
would.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from app.api.routers import webhooks_in
from app.db.models import Subscription, _normalize_billing_period
from app.payments.base import PaymentEvent


def flush_would_run(sub: Subscription) -> None:
    """What SQLAlchemy does to this row on the way to the database."""
    _normalize_billing_period(None, None, sub)


def make_sub(**overrides) -> Subscription:
    data = {
        "id": 6,
        "user_id": 10,
        "plan_id": 20,
        "status": "active",
        "source": "stripe",
        "provider": "stripe",
        "provider_subscription_id": "sub_123",
        "provider_status": "active",
        "expires_at": datetime(2026, 7, 28, tzinfo=UTC),
    }
    data.update(overrides)
    return Subscription(**data)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class WebhookDB:
    """Enough of a session for the two subscription-event handlers."""

    def __init__(self, sub):
        self.sub = sub

    async def execute(self, _query):
        return Result(self.sub)

    def add(self, _obj):
        return None

    async def flush(self):
        return None


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_the_listener_is_registered_on_both_write_paths():
    """This is what makes it unbypassable — a fourth writer inherits it."""
    assert event.contains(Subscription, "before_insert", _normalize_billing_period)
    assert event.contains(Subscription, "before_update", _normalize_billing_period)


def test_an_ordered_period_is_left_untouched():
    sub = make_sub(
        current_period_start=datetime(2026, 7, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    flush_would_run(sub)
    assert sub.current_period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert sub.current_period_end == datetime(2026, 8, 1, tzinfo=UTC)


def test_a_zero_length_period_is_not_an_error():
    """`create_or_extend_subscription`'s renewal branch yields start == end."""
    same = datetime(2026, 8, 1, tzinfo=UTC)
    sub = make_sub(current_period_start=same, current_period_end=same)
    flush_would_run(sub)
    assert sub.current_period_start == same


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, datetime(2026, 8, 1, tzinfo=UTC)),
        (datetime(2026, 8, 1, tzinfo=UTC), None),
        (None, None),
    ],
)
def test_a_half_set_period_is_left_alone(start, end):
    """Gifts and manual rows legitimately carry no period at all."""
    sub = make_sub(current_period_start=start, current_period_end=end)
    flush_would_run(sub)
    assert sub.current_period_start == start
    assert sub.current_period_end == end


def test_the_live_sub6_shape_is_clamped_and_logged(caplog):
    """The exact values measured on the live database on 2026-08-07."""
    sub = make_sub(
        current_period_start=datetime(2027, 1, 20, tzinfo=UTC),
        current_period_end=datetime(2026, 7, 28, tzinfo=UTC),
    )
    with caplog.at_level(logging.ERROR, logger="app.db.models"):
        flush_would_run(sub)

    assert sub.current_period_start == datetime(2026, 7, 28, tzinfo=UTC)
    assert sub.current_period_end == datetime(2026, 7, 28, tzinfo=UTC)
    assert sub.current_period_start <= sub.current_period_end

    message = caplog.records[0].getMessage()
    assert "running backwards" in message
    assert "2027-01-20" in message and "2026-07-28" in message


def test_the_end_is_never_moved_because_other_code_reads_it():
    """Billing notifications fall back to `current_period_end` for the renewal
    date, and reconciliation compares it against the provider. Repairing by
    moving the end would change what a member is told."""
    end = datetime(2026, 7, 28, tzinfo=UTC)
    sub = make_sub(current_period_start=datetime(2027, 1, 20, tzinfo=UTC), current_period_end=end)
    flush_would_run(sub)
    assert sub.current_period_end == end


def test_a_naive_datetime_does_not_raise():
    """Comparing naive against aware is a TypeError; a guard that crashes in a
    payment webhook is worse than the defect it guards."""
    sub = make_sub(
        current_period_start=datetime(2027, 1, 20),  # noqa: DTZ001 — the point
        current_period_end=datetime(2026, 7, 28, tzinfo=UTC),
    )
    flush_would_run(sub)
    assert sub.current_period_start == datetime(2026, 7, 28, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The two webhook shapes that produce it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stripe_event_carrying_only_an_end_cannot_invert_the_period(monkeypatch):
    """A Stripe payload whose metadata dropped the start key moves the end alone."""
    monkeypatch.setattr(webhooks_in, "cancel_pending_commission_for_referee", _noop)
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", _noop)

    sub = make_sub(
        current_period_start=datetime(2027, 1, 20, tzinfo=UTC),
        current_period_end=datetime(2027, 2, 20, tzinfo=UTC),
    )
    stripe_event = PaymentEvent(
        external_id="evt_stripe_1",
        status="subscription_updated",
        amount=0,
        currency="USD",
        metadata={
            "stripe_subscription_id": "sub_123",
            "stripe_subscription_status": "active",
            # start key absent — only the end arrives
            "stripe_subscription_current_period_end": "2026-07-28T00:00:00+00:00",
        },
    )

    await webhooks_in._handle_stripe_subscription_event(WebhookDB(sub), stripe_event)

    # The handler really does invert it — this is the defect, not a strawman.
    assert sub.current_period_start > sub.current_period_end

    flush_would_run(sub)
    assert sub.current_period_start <= sub.current_period_end
    assert sub.current_period_end == datetime(2026, 7, 28, tzinfo=UTC)


@pytest.mark.asyncio
async def test_lava_cancellation_carrying_only_will_expire_at_cannot_invert_the_period(
    monkeypatch,
):
    """Lava's `subscription.cancelled` supplies `willExpireAt` and no start."""
    monkeypatch.setattr(webhooks_in, "cancel_pending_commission_for_referee", _noop)
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", _noop)

    sub = make_sub(
        provider="lava",
        source="lava",
        provider_subscription_id="8eecb051-3a6e-4130-9efa-5e5add66ca26",
        current_period_start=datetime(2027, 1, 20, tzinfo=UTC),
        current_period_end=datetime(2027, 2, 20, tzinfo=UTC),
    )
    lava_event = PaymentEvent(
        external_id="evt_lava_1",
        status="subscription_deleted",
        amount=0,
        currency="RUB",
        metadata={
            "lava_subscription_id": "8eecb051-3a6e-4130-9efa-5e5add66ca26",
            "lava_contract_status": "cancelled",
            # start key absent — only willExpireAt arrives
            "lava_period_end": "2026-07-28T00:00:00+00:00",
        },
    )

    await webhooks_in._handle_lava_subscription_event(WebhookDB(sub), lava_event)

    assert sub.current_period_start > sub.current_period_end

    flush_would_run(sub)
    assert sub.current_period_start <= sub.current_period_end
    assert sub.current_period_end == datetime(2026, 7, 28, tzinfo=UTC)


async def _noop(*_args, **_kwargs):
    return None
