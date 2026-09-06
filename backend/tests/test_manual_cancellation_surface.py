"""GK-433: a cancellation queue nobody is told about is not a queue.

`manual_required` was being set correctly. It was written to the support feed,
it was listed in the admin panel, and it reached nobody — all three are things
you have to go and look at. sub#11 (@YelenaAlberska) sat in that state from
2026-07-24 to 2026-08-09 with a renewal due 2026-08-24, and the only reason it
surfaced at all was a manual verification pass.

These tests cover the push half: an alert the moment the provider refuses, and
a daily reminder for as long as somebody's card is still live against their
wishes. Both must carry the days remaining before the next charge — the date
alone made a row renewing in three days look like one renewing in three months.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import tasks as bot_tasks
from app.services import subscription_cancellation as cancellation
from app.services.subscription_cancellation import (
    CANCEL_MANUAL_REQUIRED,
    MANUAL_CANCELLATION_URGENT_DAYS,
    days_until,
    manual_cancellation_alert_text,
    manual_cancellation_digest_text,
    request_autorenew_cancellation,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, payment=None):
        self.payment = payment
        self.added = []

    async def execute(self, _query):
        return Result(self.payment)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def user(**overrides):
    data = {"id": 10, "tg_id": 10010, "username": "YelenaAlberska"}
    data.update(overrides)
    return SimpleNamespace(**data)


def subscription(**overrides):
    data = {
        "id": 11,
        "user_id": 10,
        "plan_id": 20,
        "status": "active",
        "source": "lava",
        "provider": "lava",
        "provider_subscription_id": None,
        "provider_status": "active",
        "expires_at": datetime(2026, 8, 24, tzinfo=UTC),
        "cancel_at_period_end": False,
        "cancel_state": None,
        "cancel_requested_at": None,
        "cancel_confirmed_at": None,
        "cancel_failure_reason": None,
        "cancel_resolved_at": None,
        "cancel_resolved_by_admin_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture
def alerts(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(cancellation, "send_ops_alert", sent)
    monkeypatch.setattr(bot_tasks, "send_ops_alert", sent)
    return sent


# ---------------------------------------------------------------------------
# the alert that fires when the provider refuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_cancellation_alerts_ops_immediately(monkeypatch, alerts):
    sub = subscription()

    async def refuse(*_args, **_kwargs):
        return "Lava contract id is unknown"

    monkeypatch.setattr(cancellation, "_dispatch_to_provider", refuse)

    outcome = await request_autorenew_cancellation(FakeSession(), sub, user=user())

    assert outcome.state == CANCEL_MANUAL_REQUIRED
    alerts.assert_awaited_once()
    body = alerts.await_args.args[0]
    assert "РУЧНАЯ ОТМЕНА" in body
    assert "@YelenaAlberska" in body
    assert "24.08.2026" in body  # when the card gets charged
    assert "Lava contract id is unknown" in body
    assert alerts.await_args.kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_pressing_the_button_twice_does_not_alert_twice(monkeypatch, alerts):
    """Rate-limited per subscription; the daily digest is what keeps it loud."""
    sub = subscription()

    async def refuse(*_args, **_kwargs):
        return "provider usdt has no autorenew cancellation API"

    monkeypatch.setattr(cancellation, "_dispatch_to_provider", refuse)

    await request_autorenew_cancellation(FakeSession(), sub, user=user())

    assert alerts.await_args.kwargs["key"] == "manual_cancel_required:11"
    assert alerts.await_args.kwargs["rate_limit_seconds"] >= 3600


@pytest.mark.asyncio
async def test_a_confirmed_cancellation_alerts_nobody(monkeypatch, alerts):
    """Only the failure is news. Alerting on success is how channels get muted."""

    async def confirm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cancellation, "_dispatch_to_provider", confirm)

    await request_autorenew_cancellation(FakeSession(), subscription(), user=user())

    alerts.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_broken_alert_channel_does_not_break_the_cancellation(monkeypatch):
    """The member's request must survive our telemetry failing."""
    monkeypatch.setattr(
        cancellation, "send_ops_alert", AsyncMock(side_effect=RuntimeError("telegram down"))
    )

    async def refuse(*_args, **_kwargs):
        return "HTTP 500"

    monkeypatch.setattr(cancellation, "_dispatch_to_provider", refuse)

    sub = subscription()
    outcome = await request_autorenew_cancellation(FakeSession(), sub, user=user())

    assert outcome.state == CANCEL_MANUAL_REQUIRED
    assert sub.cancel_state == CANCEL_MANUAL_REQUIRED


def test_the_alert_says_what_to_do_when_there_is_no_contract_id():
    """Normal for Lava RUB — no purchase webhook means no id ever reached us."""
    body = manual_cancellation_alert_text(
        subscription(provider_subscription_id=None),
        user=user(),
        provider="lava",
        reason="Lava contract id is unknown",
        payment=SimpleNamespace(id=777, note="buyer_email=member@example.com"),
        now=NOW,
    )

    assert "нет id — искать по email" in body
    assert "member@example.com" in body
    assert "subscription_id=11" in body


def test_the_alert_names_a_member_who_has_no_username():
    body = manual_cancellation_alert_text(
        subscription(),
        user=user(username=None),
        provider="lava",
        reason="HTTP 500",
        now=NOW,
    )

    assert "user #10" in body
    assert "tg 10010" in body


# ---------------------------------------------------------------------------
# the daily digest
# ---------------------------------------------------------------------------


def test_digest_leads_with_the_count_and_flags_the_urgent_ones():
    rows = [
        (subscription(id=11, expires_at=NOW + timedelta(days=3)), user()),
        (subscription(id=12, expires_at=NOW + timedelta(days=60)), user(username="other")),
    ]

    text = manual_cancellation_digest_text(rows, now=NOW)

    assert "2 в очереди" in text
    assert f"1 спишутся в ближайшие {MANUAL_CANCELLATION_URGENT_DAYS}" in text
    assert "через 3 дн." in text
    assert "через 60 дн." in text


def test_digest_does_not_paste_a_hundred_rows_into_telegram():
    rows = [
        (subscription(id=i, expires_at=NOW + timedelta(days=i)), user(username=f"u{i}"))
        for i in range(1, 26)
    ]

    text = manual_cancellation_digest_text(rows, now=NOW, max_listed=10)

    assert "25 в очереди" in text
    assert "и ещё 15" in text
    assert text.count("•") == 11  # 10 listed + the overflow line


def test_digest_handles_a_row_whose_charge_date_has_already_passed():
    rows = [(subscription(expires_at=NOW - timedelta(days=2)), user())]

    text = manual_cancellation_digest_text(rows, now=NOW)

    assert "уже прошла" in text


def test_days_until_is_none_when_there_is_no_date():
    assert days_until(None, now=NOW) is None


# ---------------------------------------------------------------------------
# the scheduled job
# ---------------------------------------------------------------------------


class QueueSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _query):
        return SimpleNamespace(all=lambda: self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_daily_job_alerts_while_the_queue_is_not_empty(monkeypatch, alerts):
    rows = [(subscription(expires_at=NOW + timedelta(days=2)), user())]
    monkeypatch.setattr(bot_tasks, "async_session", lambda: QueueSession(rows))

    await bot_tasks.manual_cancellation_queue_job()

    alerts.assert_awaited_once()
    assert "1 в очереди" in alerts.await_args.args[0]
    assert alerts.await_args.kwargs["key"] == "manual_cancellation_queue_daily"
    # Under 24h, or a bot restart at the wrong moment skips a whole day.
    assert alerts.await_args.kwargs["rate_limit_seconds"] < 24 * 3600


@pytest.mark.asyncio
async def test_daily_job_is_silent_when_the_queue_is_empty(monkeypatch, alerts):
    """A daily "nothing to do" is how an alert channel gets muted."""
    monkeypatch.setattr(bot_tasks, "async_session", lambda: QueueSession([]))

    await bot_tasks.manual_cancellation_queue_job()

    alerts.assert_not_awaited()


def test_the_job_is_actually_scheduled(monkeypatch):
    """An unscheduled job is the same amount of visibility as none at all."""
    from app.bot import main as bot_main

    jobs = []

    class FakeScheduler:
        def add_job(self, func, *_args, **kwargs):
            jobs.append((func, kwargs.get("id")))

    monkeypatch.setattr(bot_main, "AsyncIOScheduler", lambda **_kw: FakeScheduler())

    bot_main._build_scheduler(bot=None)

    assert ("manual_cancellation_queue") in [job_id for _f, job_id in jobs]
    assert bot_tasks.manual_cancellation_queue_job in [f for f, _id in jobs]


# ---------------------------------------------------------------------------
# the queue definition is shared, so the three surfaces cannot disagree
# ---------------------------------------------------------------------------


def test_api_dashboard_and_alert_all_use_one_definition_of_open():
    """A counter that disagrees with the list it links to gets ignored."""
    from app.api.routers import metrics, subscriptions

    assert (
        subscriptions.open_manual_cancellation_filters
        is cancellation.open_manual_cancellation_filters
    )
    assert (
        metrics.open_manual_cancellations_query is cancellation.open_manual_cancellations_query
    )


def test_open_queue_excludes_rows_a_provider_webhook_already_resolved():
    """Compiled SQL, so a future refactor cannot quietly drop a condition."""
    sql = str(
        cancellation.open_manual_cancellations_query().compile(
            compile_kwargs={"literal_binds": True}
        )
    )

    assert f"subscriptions.cancel_state = '{CANCEL_MANUAL_REQUIRED}'" in sql
    assert "subscriptions.cancel_resolved_at IS NULL" in sql
    assert "subscriptions.cancel_at_period_end IS false" in sql
    assert "provider_status NOT IN" in sql
    # Soonest charge first — the only ordering this queue's deadline supports.
    assert "ORDER BY subscriptions.expires_at ASC" in sql
