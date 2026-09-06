from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.bot import tasks
from app.services import subscription as subscription_service
from app.services.channel_access import SubscriptionAccessRevokeResult
from app.services.subscription import (
    create_or_extend_subscription,
    expiring_manual_usdt,
    has_subscription_access,
    is_manual_usdt_expiry_reminder_due,
    record_access_revoke_attempt,
    should_revoke_access,
    start_provider_grace,
)

NOW = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)


async def _no_banned_pending(_session):
    """GK-400: default the durable ban-revoke set to empty for expiry-focused tests."""
    return []


def armed_settings(**overrides):
    """GK-460: the expiry sweep now needs an arm flag and a chat binding of its
    own, on top of GK-443's hold. These tests are about what the sweep does once
    it is allowed to run; the arming itself is `test_expiry_removal_arming.py`."""
    data = {
        "enable_prelaunch_hold": False,
        "enable_expiry_removals": True,
        "expiry_removals_expect_chat_ids": frozenset({-1001, -1002}),
        "private_channel_id": -1001,
        "practice_chat_id": -1002,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_subscription(**overrides):
    data = {
        "id": 1,
        "status": "active",
        "source": "manual",
        "provider": None,
        "provider_subscription_id": None,
        "provider_status": None,
        "current_period_end": None,
        "expires_at": NOW + timedelta(days=10),
        "notified_expiring": False,
        "grace_started_at": None,
        "grace_ends_at": None,
        "cancel_at_period_end": False,
        "access_revoke_attempted_at": None,
        "access_revoked_at": None,
        "access_revoke_retry_after_at": None,
        "access_revoke_attempts": 0,
        "access_revoke_error": None,
        "invite_link": "https://t.me/+stored",
        "user": SimpleNamespace(tg_id=1001),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_manual_usdt_expired_subscription_revoke_marks_expired():
    sub = make_subscription(source="usdt", expires_at=NOW - timedelta(seconds=1))

    assert should_revoke_access(sub, NOW) is True

    record_access_revoke_attempt(sub, success=True, now=NOW)

    assert sub.status == "expired"
    assert sub.access_revoked_at == NOW
    assert sub.access_revoke_attempted_at == NOW
    assert sub.access_revoke_attempts == 1


def test_provider_active_current_period_future_keeps_access_even_if_local_expires_at_passed():
    sub = make_subscription(
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_123",
        provider_status="active",
        expires_at=NOW - timedelta(days=1),
        current_period_end=NOW + timedelta(days=5),
    )

    assert has_subscription_access(sub, NOW) is True
    assert should_revoke_access(sub, NOW) is False


def test_past_due_subscription_in_grace_keeps_access():
    sub = make_subscription(
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_123",
        provider_status="past_due",
        current_period_end=NOW - timedelta(days=1),
        grace_started_at=NOW - timedelta(days=1),
        grace_ends_at=NOW + timedelta(days=2),
    )

    assert has_subscription_access(sub, NOW) is True
    assert should_revoke_access(sub, NOW) is False


def test_past_due_subscription_after_grace_requires_revoke():
    sub = make_subscription(
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_123",
        provider_status="past_due",
        current_period_end=NOW - timedelta(days=4),
        grace_started_at=NOW - timedelta(days=3),
        grace_ends_at=NOW - timedelta(seconds=1),
    )

    assert has_subscription_access(sub, NOW) is False
    assert should_revoke_access(sub, NOW) is True


def test_cancel_at_period_end_waits_for_paid_period_and_grace():
    sub = make_subscription(
        status="cancelled",
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_123",
        provider_status="canceled",
        cancel_at_period_end=True,
        current_period_end=NOW + timedelta(days=1),
    )

    assert has_subscription_access(sub, NOW) is True
    assert should_revoke_access(sub, NOW) is False

    sub.current_period_end = NOW - timedelta(days=1)
    sub.grace_ends_at = NOW + timedelta(days=1)

    assert has_subscription_access(sub, NOW) is True
    assert should_revoke_access(sub, NOW) is False

    sub.grace_ends_at = NOW - timedelta(seconds=1)

    assert has_subscription_access(sub, NOW) is False
    assert should_revoke_access(sub, NOW) is True


def test_retry_after_delays_next_revoke_attempt():
    sub = make_subscription(
        expires_at=NOW - timedelta(days=1),
        access_revoke_retry_after_at=NOW + timedelta(seconds=30),
    )

    assert should_revoke_access(sub, NOW) is False

    sub.access_revoke_retry_after_at = NOW - timedelta(seconds=1)

    assert should_revoke_access(sub, NOW) is True


def test_failed_renewal_enters_three_day_grace_from_now():
    sub = make_subscription(
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_123",
        provider_status="active",
        current_period_end=NOW - timedelta(hours=1),
    )

    start_provider_grace(sub, now=NOW)

    assert sub.provider_status == "past_due"
    assert sub.grace_started_at == NOW
    assert sub.grace_ends_at == NOW + timedelta(days=3)
    assert has_subscription_access(sub, NOW) is True


def test_manual_usdt_reminder_includes_exact_three_day_boundary():
    sub = make_subscription(
        source="usdt",
        provider="usdt",
        expires_at=NOW + timedelta(days=3),
    )

    assert is_manual_usdt_expiry_reminder_due(sub, now=NOW) is True

    sub.expires_at = NOW + timedelta(days=3, seconds=1)
    assert is_manual_usdt_expiry_reminder_due(sub, now=NOW) is False

    sub.expires_at = NOW
    assert is_manual_usdt_expiry_reminder_due(sub, now=NOW) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "stripe", "provider": "stripe", "provider_subscription_id": "sub_1"},
        {"source": "lava", "provider": "lava", "provider_subscription_id": "lava_1"},
        {"source": "manual", "provider": None},
        {"source": "zelle", "provider": "zelle"},
        {"source": "usdt", "provider": "stripe"},
        {"source": "usdt", "provider": "usdt", "provider_subscription_id": "recurring_1"},
        {"source": "usdt", "provider": "usdt", "notified_expiring": True},
        {"source": "usdt", "provider": "usdt", "status": "expired"},
    ],
)
def test_manual_usdt_reminder_excludes_autorenew_and_unrelated_sources(overrides):
    sub = make_subscription(expires_at=NOW + timedelta(days=2), **overrides)

    assert is_manual_usdt_expiry_reminder_due(sub, now=NOW) is False


@pytest.mark.asyncio
async def test_expiring_manual_usdt_filters_candidates_defensively():
    eligible = make_subscription(
        id=1,
        source="usdt",
        provider="usdt",
        expires_at=NOW + timedelta(days=2),
    )
    stripe = make_subscription(
        id=2,
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_2",
        expires_at=NOW + timedelta(days=2),
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [eligible, stripe]

    class FakeSession:
        async def execute(self, _query):
            return Result()

    rows = await expiring_manual_usdt(FakeSession(), now=NOW)

    assert rows == [eligible]


@pytest.mark.asyncio
async def test_usdt_renewal_advances_period_and_rearms_reminder(monkeypatch):
    current = make_subscription(
        source="usdt",
        provider="usdt",
        expires_at=NOW + timedelta(days=2),
        current_period_end=NOW + timedelta(days=2),
        notified_expiring=True,
        plan_id=1,
    )

    async def fake_active_subscription(_session, _user_id):
        return current

    monkeypatch.setattr(subscription_service, "get_active_subscription", fake_active_subscription)

    result = await create_or_extend_subscription(
        SimpleNamespace(),
        user=SimpleNamespace(id=10),
        plan=SimpleNamespace(id=2, duration_days=30),
        source="usdt",
        provider="usdt",
        current_period_start=NOW + timedelta(days=2),
        current_period_end=NOW + timedelta(days=32),
    )

    assert result is current
    assert current.expires_at == NOW + timedelta(days=32)
    assert current.notified_expiring is False


@pytest.mark.asyncio
async def test_expiry_reminder_success_is_idempotent_across_daily_runs(monkeypatch):
    sub = make_subscription(
        source="usdt",
        provider="usdt",
        expires_at=NOW + timedelta(days=2),
    )
    deliveries = []

    class FakeSession:
        commits = 0

        async def commit(self):
            self.commits += 1

    class SessionContext:
        def __init__(self):
            self.session = FakeSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    context = SessionContext()

    async def fake_expiring(_session, within_days):
        assert within_days == 3
        return [] if sub.notified_expiring else [sub]

    async def fake_notify(tg_id, expires_at, *, reply_markup):
        deliveries.append((tg_id, expires_at, reply_markup))
        return True

    monkeypatch.setattr(tasks, "async_session", lambda: context)
    monkeypatch.setattr(tasks, "expiring_manual_usdt", fake_expiring)
    monkeypatch.setattr(tasks, "notify_usdt_expiring", fake_notify)

    await tasks.remind_expiring_job()
    await tasks.remind_expiring_job()

    assert len(deliveries) == 1
    assert sub.notified_expiring is True
    assert context.session.commits == 1


@pytest.mark.asyncio
async def test_failed_expiry_reminder_retries_on_next_daily_run(monkeypatch):
    sub = make_subscription(
        source="usdt",
        provider="usdt",
        expires_at=NOW + timedelta(days=2),
    )
    delivery_results = iter([False, True])
    attempts = 0

    class FakeSession:
        commits = 0

        async def commit(self):
            self.commits += 1

    class SessionContext:
        def __init__(self):
            self.session = FakeSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    context = SessionContext()

    async def fake_expiring(_session, within_days):
        assert within_days == 3
        return [] if sub.notified_expiring else [sub]

    async def fake_notify(_tg_id, _expires_at, *, reply_markup):
        nonlocal attempts
        attempts += 1
        assert reply_markup is not None
        return next(delivery_results)

    monkeypatch.setattr(tasks, "async_session", lambda: context)
    monkeypatch.setattr(tasks, "expiring_manual_usdt", fake_expiring)
    monkeypatch.setattr(tasks, "notify_usdt_expiring", fake_notify)

    await tasks.remind_expiring_job()
    assert sub.notified_expiring is False
    assert context.session.commits == 0

    await tasks.remind_expiring_job()
    assert attempts == 2
    assert sub.notified_expiring is True
    assert context.session.commits == 1


@pytest.mark.asyncio
async def test_kick_expired_job_records_telegram_retry_after(monkeypatch):
    sub = make_subscription(expires_at=NOW - timedelta(days=1))

    class FakeSession:
        commits = 0

        async def refresh(self, *_args):
            raise AssertionError("user was already loaded")

        async def commit(self):
            self.commits += 1

    class SessionContext:
        def __init__(self):
            self.session = FakeSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    session_context = SessionContext()

    async def fake_expire(_session):
        return [sub]

    async def fake_revoke(_bot, _tg_id, _invite_link):
        return SubscriptionAccessRevokeResult(False, retry_after=42, error="telegram retry")

    monkeypatch.setattr(tasks, "async_session", lambda: session_context)
    monkeypatch.setattr(tasks, "get_settings", lambda: armed_settings())
    monkeypatch.setattr(tasks, "expire_subscriptions", fake_expire)
    monkeypatch.setattr(tasks, "subscriptions_pending_ban_revoke", _no_banned_pending)
    monkeypatch.setattr(tasks, "revoke_subscription_access", fake_revoke)
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)

    await tasks.kick_expired_job(SimpleNamespace())

    assert sub.status == "active"
    assert sub.access_revoked_at is None
    assert sub.access_revoke_retry_after_at == NOW + timedelta(seconds=42)
    assert sub.access_revoke_error == "telegram retry"
    assert sub.access_revoke_attempts == 1
    assert session_context.session.commits == 1


@pytest.mark.asyncio
async def test_kick_expired_job_revokes_invite_link_and_kicks_user(monkeypatch):
    sub = make_subscription(expires_at=NOW - timedelta(days=1), invite_link="bare-link")
    calls = []

    class FakeSession:
        commits = 0

        async def refresh(self, *_args):
            raise AssertionError("user was already loaded")

        async def commit(self):
            self.commits += 1

    class SessionContext:
        def __init__(self):
            self.session = FakeSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    session_context = SessionContext()

    async def fake_expire(_session):
        return [sub]

    async def fake_revoke(_bot, tg_id, invite_link):
        calls.append((tg_id, invite_link))
        return SubscriptionAccessRevokeResult(True)

    monkeypatch.setattr(tasks, "async_session", lambda: session_context)
    monkeypatch.setattr(tasks, "get_settings", lambda: armed_settings())
    monkeypatch.setattr(tasks, "expire_subscriptions", fake_expire)
    monkeypatch.setattr(tasks, "subscriptions_pending_ban_revoke", _no_banned_pending)
    monkeypatch.setattr(tasks, "revoke_subscription_access", fake_revoke)
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)

    await tasks.kick_expired_job(SimpleNamespace())

    assert calls == [(1001, "bare-link")]
    assert sub.status == "expired"
    assert sub.access_revoked_at == NOW
    assert session_context.session.commits == 1


@pytest.mark.asyncio
async def test_kick_expired_job_continues_after_one_revoke_crash(monkeypatch):
    first = make_subscription(
        id=1,
        expires_at=NOW - timedelta(days=1),
        user=SimpleNamespace(tg_id=1001),
    )
    second = make_subscription(
        id=2,
        expires_at=NOW - timedelta(days=1),
        user=SimpleNamespace(tg_id=1002),
    )
    calls = []

    class FakeSession:
        commits = 0

        async def refresh(self, *_args):
            raise AssertionError("user was already loaded")

        async def commit(self):
            self.commits += 1

    class SessionContext:
        def __init__(self):
            self.session = FakeSession()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    session_context = SessionContext()

    async def fake_expire(_session):
        return [first, second]

    async def fake_revoke(_bot, tg_id, _invite_link):
        calls.append(tg_id)
        if tg_id == 1001:
            raise RuntimeError("telegram transport exploded")
        return SubscriptionAccessRevokeResult(True)

    monkeypatch.setattr(tasks, "async_session", lambda: session_context)
    monkeypatch.setattr(tasks, "get_settings", lambda: armed_settings())
    monkeypatch.setattr(tasks, "expire_subscriptions", fake_expire)
    monkeypatch.setattr(tasks, "subscriptions_pending_ban_revoke", _no_banned_pending)
    monkeypatch.setattr(tasks, "revoke_subscription_access", fake_revoke)
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)

    await tasks.kick_expired_job(SimpleNamespace())

    assert calls == [1001, 1002]
    assert first.status == "active"
    assert first.access_revoked_at is None
    assert "telegram transport exploded" in first.access_revoke_error
    assert second.status == "expired"
    assert second.access_revoked_at == NOW
    assert session_context.session.commits == 2
