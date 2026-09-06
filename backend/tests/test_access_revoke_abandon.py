"""GK-432: a removal the bot can never finish has to end somewhere.

`sub#2` on the live host recorded **647** failed access-revoke attempts with
`access_revoked_at` still NULL. Telegram's answer was the same every hour —
`can't remove chat owner` — and the hourly job kept asking: 48 warning lines a
day, no alert to anyone, and a row that read like an attempt still in progress.

These tests pin the three things that fixes it: a refusal Telegram will never
change is recognised as permanent and stops immediately, an error that might
clear keeps its retries, and either way the retrying ends at a bound with
exactly one alert naming the member who is still sitting in the channel.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import BanChatMember

from app.bot import tasks
from app.services import channel_access
from app.services import subscription as subscription_service
from app.services.channel_access import SubscriptionAccessRevokeResult
from app.services.subscription import (
    MAX_ACCESS_REVOKE_ATTEMPTS,
    record_access_revoke_attempt,
    should_revoke_access,
)

NOW = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)

CHAT_OWNER = "Telegram server says - Bad Request: can't remove chat owner"


def make_subscription(**overrides):
    data = {
        "id": 1,
        "status": "active",
        "source": "manual",
        "provider": None,
        "provider_subscription_id": None,
        "provider_status": None,
        "current_period_end": None,
        "expires_at": NOW - timedelta(days=1),
        "notified_expiring": False,
        "grace_started_at": None,
        "grace_ends_at": None,
        "cancel_at_period_end": False,
        "access_revoke_attempted_at": None,
        "access_revoked_at": None,
        "access_revoke_retry_after_at": None,
        "access_revoke_attempts": 0,
        "access_revoke_error": None,
        "access_revoke_abandoned_at": None,
        "invite_link": None,
        "user": SimpleNamespace(tg_id=1001),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def refresh(self, *_args):
        raise AssertionError("user was already loaded")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class SessionContext:
    def __init__(self):
        self.session = FakeSession()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


async def _no_banned_pending(_session):
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


def install_job(monkeypatch, *, due, revoke, banned=None):
    """Wire ``kick_expired_job`` to in-memory rows and a scripted Telegram."""
    context = SessionContext()
    alerts: list[dict] = []

    async def fake_expire(_session):
        # Mirrors the real query's final filter, so a row the service considers
        # settled genuinely stops being handed to the job.
        return [sub for sub in due if should_revoke_access(sub, NOW)]

    async def fake_alert(text, *, key=None, rate_limit_seconds=300, severity="warn"):
        alerts.append({"text": text, "key": key, "severity": severity})
        return True

    monkeypatch.setattr(tasks, "get_settings", lambda: armed_settings())
    monkeypatch.setattr(tasks, "async_session", lambda: context)
    monkeypatch.setattr(tasks, "expire_subscriptions", fake_expire)
    monkeypatch.setattr(
        tasks,
        "subscriptions_pending_ban_revoke",
        banned or _no_banned_pending,
    )
    monkeypatch.setattr(tasks, "revoke_subscription_access", revoke)
    monkeypatch.setattr(tasks, "send_ops_alert", fake_alert)
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)
    return context, alerts


def access_settings(**overrides):
    data = {
        "private_channel_id": -1001,
        "private_channel_invite_link": "",
        "practice_chat_id": -1002,
        "practice_chat_invite_link": "",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


# --- classification -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_refusal_that_ran_647_times_is_recognised_as_permanent(monkeypatch):
    class FakeBot:
        async def ban_chat_member(self, **kwargs):
            raise TelegramBadRequest(
                method=BanChatMember(chat_id=kwargs["chat_id"], user_id=kwargs["user_id"]),
                message="Bad Request: can't remove chat owner",
            )

        async def unban_chat_member(self, **_kwargs):
            raise AssertionError("ban failed; unban must not run")

    monkeypatch.setattr(channel_access, "settings", access_settings())

    result = await channel_access.kick_user(FakeBot(), tg_id=7007)

    assert result.success is False
    assert result.permanent is True


@pytest.mark.asyncio
async def test_a_rate_limit_is_not_permanent(monkeypatch):
    class FakeBot:
        async def ban_chat_member(self, **kwargs):
            raise TelegramRetryAfter(
                method=BanChatMember(chat_id=kwargs["chat_id"], user_id=kwargs["user_id"]),
                message="Too Many Requests: retry after 42",
                retry_after=42,
            )

        async def unban_chat_member(self, **_kwargs):
            raise AssertionError("ban failed; unban must not run")

    monkeypatch.setattr(channel_access, "settings", access_settings())

    result = await channel_access.kick_user(FakeBot(), tg_id=7007)

    assert result.success is False
    assert result.permanent is False
    assert result.retry_after == 42


@pytest.mark.asyncio
async def test_one_transient_failure_beside_a_permanent_one_still_earns_another_hour(monkeypatch):
    # The channel refuses forever, the practice chat is merely rate-limited.
    # Giving up now would abandon a removal that is still half-achievable; the
    # attempt bound is what ends it if the rate limit never clears.
    monkeypatch.setattr(channel_access, "settings", access_settings())

    async def fake_kick(_bot, _tg_id, resource):
        if resource.key == "community_channel":
            return channel_access.KickResourceResult(
                resource, False, error=CHAT_OWNER, permanent=True
            )
        return channel_access.KickResourceResult(
            resource, False, retry_after=42, error="retry_after=42"
        )

    monkeypatch.setattr(channel_access, "_kick_user_from_resource", fake_kick)

    result = await channel_access.kick_user(SimpleNamespace(), tg_id=7007)

    assert result.success is False
    assert result.permanent is False


# --- the job --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_permanent_refusal_stops_at_one_attempt_and_alerts_exactly_once(monkeypatch):
    sub = make_subscription(id=2)

    async def fake_revoke(_bot, _tg_id, _invite_link):
        return SubscriptionAccessRevokeResult(False, error=CHAT_OWNER, permanent=True)

    context, alerts = install_job(monkeypatch, due=[sub], revoke=fake_revoke)

    await tasks.kick_expired_job(SimpleNamespace())
    await tasks.kick_expired_job(SimpleNamespace())
    await tasks.kick_expired_job(SimpleNamespace())

    assert sub.access_revoke_attempts == 1
    assert sub.access_revoke_abandoned_at == NOW
    assert sub.access_revoked_at is None
    assert should_revoke_access(sub, NOW) is False
    assert len(alerts) == 1
    assert alerts[0]["key"] == "access_revoke_abandoned_2"
    assert alerts[0]["severity"] == "error"
    assert "subscription #2" in alerts[0]["text"]
    assert "1001" in alerts[0]["text"]
    assert context.session.commits == 1


@pytest.mark.asyncio
async def test_a_transient_error_still_retries_and_eventually_succeeds(monkeypatch):
    sub = make_subscription(id=3)
    outcomes = iter(
        [
            SubscriptionAccessRevokeResult(False, error="connection reset"),
            SubscriptionAccessRevokeResult(False, error="connection reset"),
            SubscriptionAccessRevokeResult(True),
        ]
    )

    async def fake_revoke(_bot, _tg_id, _invite_link):
        return next(outcomes)

    context, alerts = install_job(monkeypatch, due=[sub], revoke=fake_revoke)

    async def fake_send_message(*_args, **_kwargs):
        return None

    bot = SimpleNamespace(send_message=fake_send_message)

    await tasks.kick_expired_job(bot)
    assert sub.access_revoke_attempts == 1
    assert sub.access_revoke_abandoned_at is None
    assert should_revoke_access(sub, NOW) is True

    await tasks.kick_expired_job(bot)
    await tasks.kick_expired_job(bot)

    assert sub.access_revoke_attempts == 3
    assert sub.access_revoked_at == NOW
    assert sub.access_revoke_abandoned_at is None
    assert sub.access_revoke_error is None
    assert sub.status == "expired"
    assert alerts == []
    assert context.session.commits == 3


@pytest.mark.asyncio
async def test_an_endless_transient_failure_ends_at_the_bound(monkeypatch):
    # This is `sub#2`'s shape with the cause hidden behind a generic error: the
    # refusal is not on the permanent list, so only the bound can stop it. It
    # must reach a terminal state instead of accumulating attempt 648.
    sub = make_subscription(id=2)

    async def fake_revoke(_bot, _tg_id, _invite_link):
        return SubscriptionAccessRevokeResult(False, error="Bad Request: something new")

    _context, alerts = install_job(monkeypatch, due=[sub], revoke=fake_revoke)

    for _ in range(MAX_ACCESS_REVOKE_ATTEMPTS + 5):
        await tasks.kick_expired_job(SimpleNamespace())

    assert sub.access_revoke_attempts == MAX_ACCESS_REVOKE_ATTEMPTS
    assert sub.access_revoke_abandoned_at == NOW
    assert sub.access_revoke_retry_after_at is None
    assert len(alerts) == 1
    assert "retry limit reached" in alerts[0]["text"]


@pytest.mark.asyncio
async def test_a_member_the_bot_cannot_remove_does_not_stop_the_batch(monkeypatch):
    stuck = make_subscription(id=2, user=SimpleNamespace(tg_id=1001))
    ordinary = make_subscription(id=4, user=SimpleNamespace(tg_id=1002))
    seen = []

    async def fake_revoke(_bot, tg_id, _invite_link):
        seen.append(tg_id)
        if tg_id == 1001:
            return SubscriptionAccessRevokeResult(False, error=CHAT_OWNER, permanent=True)
        return SubscriptionAccessRevokeResult(True)

    async def fake_send_message(*_args, **_kwargs):
        return None

    _context, alerts = install_job(monkeypatch, due=[stuck, ordinary], revoke=fake_revoke)

    await tasks.kick_expired_job(SimpleNamespace(send_message=fake_send_message))

    assert seen == [1001, 1002]
    assert stuck.access_revoke_abandoned_at == NOW
    assert ordinary.access_revoked_at == NOW
    assert ordinary.status == "expired"
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_an_alert_crash_does_not_lose_the_recorded_give_up(monkeypatch):
    sub = make_subscription(id=2)

    async def fake_revoke(_bot, _tg_id, _invite_link):
        return SubscriptionAccessRevokeResult(False, error=CHAT_OWNER, permanent=True)

    context, _alerts = install_job(monkeypatch, due=[sub], revoke=fake_revoke)

    async def exploding_alert(*_args, **_kwargs):
        raise RuntimeError("alert chat is unreachable")

    monkeypatch.setattr(tasks, "send_ops_alert", exploding_alert)

    await tasks.kick_expired_job(SimpleNamespace())

    # The commit happens before the alert, and the alert failure is swallowed —
    # an unreachable alert chat must not roll the terminal state back into an
    # eternal retry.
    assert sub.access_revoke_abandoned_at == NOW
    assert context.session.commits == 1
    assert context.session.rollbacks == 0


@pytest.mark.asyncio
async def test_a_banned_member_the_bot_cannot_remove_is_abandoned_without_expiring(monkeypatch):
    sub = make_subscription(
        id=5,
        expires_at=NOW + timedelta(days=20),
        user=SimpleNamespace(tg_id=1005),
    )

    async def fake_banned(_session):
        return [sub] if sub.access_revoke_abandoned_at is None else []

    async def fake_revoke(_bot, _tg_id, _invite_link):
        return SubscriptionAccessRevokeResult(False, error=CHAT_OWNER, permanent=True)

    _context, alerts = install_job(monkeypatch, due=[], revoke=fake_revoke, banned=fake_banned)

    await tasks.kick_expired_job(SimpleNamespace())
    await tasks.kick_expired_job(SimpleNamespace())

    assert sub.access_revoke_abandoned_at == NOW
    # A ban must not rewrite a still-paid subscription as expired (GK-400).
    assert sub.status == "active"
    assert len(alerts) == 1
    assert "banned member" in alerts[0]["text"]


# --- the record ------------------------------------------------------------


def test_giving_up_is_reported_once_and_only_once():
    sub = make_subscription(id=2)

    assert record_access_revoke_attempt(sub, success=False, permanent=True, now=NOW) is True
    later = NOW + timedelta(hours=1)
    assert record_access_revoke_attempt(sub, success=False, permanent=True, now=later) is False
    assert sub.access_revoke_abandoned_at == NOW


def test_a_later_success_clears_the_give_up():
    # A human fixes the cause — demotes the owner, restores the bot's rights —
    # and the next manual attempt has to be able to finish the removal.
    sub = make_subscription(id=2, access_revoke_abandoned_at=NOW, access_revoke_attempts=1)

    record_access_revoke_attempt(sub, success=True, now=NOW + timedelta(days=1))

    assert sub.access_revoke_abandoned_at is None
    assert sub.access_revoked_at == NOW + timedelta(days=1)
    assert sub.access_revoke_error is None
    assert sub.status == "expired"
