"""GK-400: bans must be enforced across every Telegram surface.

Covers the bot middleware deny (banned users are never dispatched to handlers),
the durable ban-revoke retry in the hourly kick job, and the admin ban/unban
action (immediate, best-effort Telegram revoke on ban; entitlement-aware
fresh-invite restore on unban). Fully mocked — no DB/Redis.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


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


def make_sub(**overrides):
    data = {
        "id": 1,
        "status": "active",
        "source": "manual",
        "provider": None,
        "provider_subscription_id": None,
        "provider_status": None,
        "current_period_end": None,
        "expires_at": NOW + timedelta(days=20),
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


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeUserSession:
    """Minimal aiogram-middleware session: one user lookup."""

    def __init__(self, user):
        self._user = user
        self.flushed = False

    async def execute(self, _query):
        return _ScalarResult(self._user)

    def add(self, _obj):  # pragma: no cover - new users not exercised here
        pass

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_banned_user_update_is_not_dispatched_to_handler():
    from app.bot.middlewares.user import UserMiddleware

    banned = SimpleNamespace(
        id=1, tg_id=1001, is_banned=True, username="u", first_name="f", last_name=None
    )
    session = _FakeUserSession(banned)
    tg_user = SimpleNamespace(
        id=1001, username="u", first_name="f", last_name=None, language_code="ru"
    )
    data = {"event_from_user": tg_user, "session": session}

    called = False

    async def handler(_event, _data):
        nonlocal called
        called = True
        return "dispatched"

    result = await UserMiddleware()(handler, SimpleNamespace(), data)

    assert called is False
    assert result is None


@pytest.mark.asyncio
async def test_active_user_update_is_dispatched():
    from app.bot.middlewares.user import UserMiddleware

    active = SimpleNamespace(
        id=2, tg_id=2002, is_banned=False, username="u2", first_name="f2", last_name=None
    )
    session = _FakeUserSession(active)
    tg_user = SimpleNamespace(
        id=2002, username="u2", first_name="f2", last_name=None, language_code="ru"
    )
    data = {"event_from_user": tg_user, "session": session}

    called = False

    async def handler(_event, _data):
        nonlocal called
        called = True
        return "dispatched"

    result = await UserMiddleware()(handler, SimpleNamespace(), data)

    assert called is True
    assert result == "dispatched"
    assert data["user"] is active


# --- Part C: durable ban-revoke (subscription service + hourly kick job) --------


def test_record_access_revoke_attempt_keeps_status_when_not_expiring():
    from app.services.subscription import record_access_revoke_attempt

    sub = make_sub(status="active", expires_at=NOW + timedelta(days=20))

    record_access_revoke_attempt(sub, success=True, mark_status_expired=False, now=NOW)

    assert sub.access_revoked_at == NOW
    assert sub.access_revoke_attempts == 1
    # A ban revoke must NOT expire a still-paid subscription, so unban can restore it.
    assert sub.status == "active"


def test_record_access_revoke_attempt_still_expires_by_default():
    from app.services.subscription import record_access_revoke_attempt

    sub = make_sub(status="active", expires_at=NOW - timedelta(days=1))

    record_access_revoke_attempt(sub, success=True, now=NOW)

    assert sub.access_revoked_at == NOW
    assert sub.status == "expired"


@pytest.mark.asyncio
async def test_subscriptions_pending_ban_revoke_filters_banned_and_retry_timing():
    from app.services.subscription import subscriptions_pending_ban_revoke

    due = make_sub(id=1, access_revoke_retry_after_at=None)
    backoff = make_sub(id=2, access_revoke_retry_after_at=NOW + timedelta(seconds=90))

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [due, backoff]

    captured = {}

    class FakeSession:
        async def execute(self, query):
            captured["sql"] = str(query.compile(compile_kwargs={"literal_binds": True}))
            return Result()

    rows = await subscriptions_pending_ban_revoke(FakeSession(), now=NOW)

    # SQL restricts to banned users with access not yet revoked.
    assert "is_banned" in captured["sql"]
    assert "access_revoked_at IS NULL" in captured["sql"]
    # The future retry-after row is held back; the due row is returned.
    assert rows == [due]


@pytest.mark.asyncio
async def test_kick_expired_job_revokes_banned_member_without_expiring(monkeypatch):
    from app.bot import tasks
    from app.services import subscription as subscription_service
    from app.services.channel_access import SubscriptionAccessRevokeResult

    banned_sub = make_sub(
        id=10,
        status="active",
        expires_at=NOW + timedelta(days=20),
        user=SimpleNamespace(tg_id=5005),
        invite_link="ban-link",
    )
    calls = []
    messages = []

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

    ctx = SessionContext()

    async def fake_expire(_session):
        return []

    async def fake_pending(_session):
        return [banned_sub]

    async def fake_revoke(_bot, tg_id, invite_link):
        calls.append((tg_id, invite_link))
        return SubscriptionAccessRevokeResult(True)

    bot = SimpleNamespace(send_message=lambda *a, **k: messages.append(a))

    monkeypatch.setattr(tasks, "async_session", lambda: ctx)
    monkeypatch.setattr(tasks, "get_settings", lambda: armed_settings())
    monkeypatch.setattr(tasks, "expire_subscriptions", fake_expire)
    monkeypatch.setattr(tasks, "subscriptions_pending_ban_revoke", fake_pending)
    monkeypatch.setattr(tasks, "revoke_subscription_access", fake_revoke)
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)

    await tasks.kick_expired_job(bot)

    assert calls == [(5005, "ban-link")]
    assert banned_sub.access_revoked_at == NOW
    # A ban does not "expire" the paid subscription.
    assert banned_sub.status == "active"
    # No "your subscription ended" notice for a banned member.
    assert messages == []
    assert ctx.session.commits == 1


@pytest.mark.asyncio
async def test_kick_expired_job_skips_banned_sub_already_in_expired_batch(monkeypatch):
    from app.bot import tasks
    from app.services import subscription as subscription_service
    from app.services.channel_access import SubscriptionAccessRevokeResult

    sub = make_sub(
        id=77,
        status="active",
        expires_at=NOW - timedelta(days=1),
        user=SimpleNamespace(tg_id=7007),
        invite_link="dup-link",
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

    ctx = SessionContext()

    async def fake_expire(_session):
        return [sub]

    async def fake_pending(_session):
        return [sub]  # same row also matches the banned query

    async def fake_revoke(_bot, tg_id, invite_link):
        calls.append((tg_id, invite_link))
        return SubscriptionAccessRevokeResult(True)

    monkeypatch.setattr(tasks, "async_session", lambda: ctx)
    monkeypatch.setattr(tasks, "get_settings", lambda: armed_settings())
    monkeypatch.setattr(tasks, "expire_subscriptions", fake_expire)
    monkeypatch.setattr(tasks, "subscriptions_pending_ban_revoke", fake_pending)
    monkeypatch.setattr(tasks, "revoke_subscription_access", fake_revoke)
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)

    await tasks.kick_expired_job(SimpleNamespace())

    # Revoked exactly once despite appearing in both batches.
    assert calls == [(7007, "dup-link")]


# --- Parts B & D: admin ban/unban action -------------------------------------


class _UserResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SubsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ActionDB:
    """Returns queued results in execute order (user lookup, then subs query)."""

    def __init__(self, *results):
        self.results = list(results)

    async def execute(self, _query):
        return self.results.pop(0)


class _FakeBot:
    def __init__(self, token):
        self.token = token
        self.messages = []
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        return None

    async def send_message(self, chat_id, text, **_kwargs):
        self.messages.append((chat_id, text))


def _patch_action_common(monkeypatch, users_router, bot_token="tok"):
    audits = []

    async def fake_audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(users_router, "settings", SimpleNamespace(bot_token=bot_token))
    monkeypatch.setattr(users_router, "audit_record", fake_audit)
    return audits


@pytest.mark.asyncio
async def test_ban_action_revokes_telegram_access_immediately(monkeypatch):
    from app.api.routers import users as users_router
    from app.api.routers.users import UserActionIn, user_action
    from app.services.channel_access import SubscriptionAccessRevokeResult

    user = SimpleNamespace(id=10, tg_id=9001, is_banned=False)
    sub = make_sub(id=1, status="active", invite_link="old-link", user=user)
    revoke_calls = []

    async def fake_revoke(_bot, tg_id, invite_link):
        revoke_calls.append((tg_id, invite_link))
        return SubscriptionAccessRevokeResult(True)

    _patch_action_common(monkeypatch, users_router)
    monkeypatch.setattr(users_router, "Bot", _FakeBot)
    monkeypatch.setattr(users_router, "revoke_subscription_access", fake_revoke)

    db = _ActionDB(_UserResult(user), _SubsResult([sub]))
    result = await user_action(
        10, UserActionIn(action="ban"), db, SimpleNamespace(id=7), SimpleNamespace()
    )

    assert user.is_banned is True
    assert revoke_calls == [(9001, "old-link")]
    # Recorded on the subscription, but the paid subscription is NOT expired.
    assert sub.access_revoked_at is not None
    assert sub.status == "active"
    assert result["ok"] is True
    assert result["access_revoked"] == 1
    assert result["access_revoke_failed"] == 0


@pytest.mark.asyncio
async def test_ban_action_surfaces_partial_revoke_failure(monkeypatch):
    from app.api.routers import users as users_router
    from app.api.routers.users import UserActionIn, user_action
    from app.services.channel_access import SubscriptionAccessRevokeResult

    user = SimpleNamespace(id=11, tg_id=9002, is_banned=False)
    sub = make_sub(id=2, status="active", invite_link="x", user=user)

    async def fake_revoke(_bot, _tg_id, _invite_link):
        return SubscriptionAccessRevokeResult(False, error="telegram down")

    _patch_action_common(monkeypatch, users_router)
    monkeypatch.setattr(users_router, "Bot", _FakeBot)
    monkeypatch.setattr(users_router, "revoke_subscription_access", fake_revoke)

    db = _ActionDB(_UserResult(user), _SubsResult([sub]))
    result = await user_action(
        11, UserActionIn(action="ban"), db, SimpleNamespace(id=7), SimpleNamespace()
    )

    assert user.is_banned is True
    # Failed revoke leaves access_revoked_at NULL so the hourly job retries.
    assert sub.access_revoked_at is None
    assert result["access_revoked"] == 0
    assert result["access_revoke_failed"] == 1


@pytest.mark.asyncio
async def test_unban_restores_entitled_subscription_with_fresh_invite(monkeypatch):
    from app.api.routers import users as users_router
    from app.api.routers.users import UserActionIn, user_action

    user = SimpleNamespace(id=12, tg_id=9003, is_banned=True)
    # Ban-revoked but still inside the paid window.
    sub = make_sub(
        id=3,
        status="active",
        expires_at=NOW + timedelta(days=15),
        access_revoked_at=NOW,
        invite_link="old-ban-link",
        user=user,
    )

    async def fake_create(_bot, name=None):
        return SimpleNamespace(any_success=True, storage_text="https://t.me/+fresh")

    _patch_action_common(monkeypatch, users_router)
    monkeypatch.setattr(users_router, "Bot", _FakeBot)
    monkeypatch.setattr(users_router, "create_invite_links", fake_create)
    # Freeze the entitlement clock to the fixture's NOW. The restore path checks
    # the paid window with users_router.utcnow(); without this the fixed NOW-based
    # fixtures drift past the real wall clock and the test becomes a date-bomb.
    monkeypatch.setattr(users_router, "utcnow", lambda: NOW)

    db = _ActionDB(_UserResult(user), _SubsResult([sub]))
    result = await user_action(
        12, UserActionIn(action="unban"), db, SimpleNamespace(id=7), SimpleNamespace()
    )

    assert user.is_banned is False
    # Portal access restored and a FRESH invite issued (never the old link).
    assert sub.access_revoked_at is None
    assert sub.invite_link == "https://t.me/+fresh"
    assert result["access_restored"] == 1


@pytest.mark.asyncio
async def test_unban_does_not_restore_subscription_expired_during_ban(monkeypatch):
    from app.api.routers import users as users_router
    from app.api.routers.users import UserActionIn, user_action

    user = SimpleNamespace(id=13, tg_id=9004, is_banned=True)
    # Paid window already elapsed while banned — must NOT be restored.
    sub = make_sub(
        id=4,
        status="active",
        expires_at=NOW - timedelta(days=1),
        access_revoked_at=NOW - timedelta(days=2),
        invite_link="old-ban-link",
        user=user,
    )
    create_calls = []

    async def fake_create(_bot, name=None):
        create_calls.append(name)
        return SimpleNamespace(any_success=True, storage_text="https://t.me/+fresh")

    _patch_action_common(monkeypatch, users_router)
    monkeypatch.setattr(users_router, "Bot", _FakeBot)
    monkeypatch.setattr(users_router, "create_invite_links", fake_create)
    # Freeze the entitlement clock to the fixture's NOW. The restore path checks
    # the paid window with users_router.utcnow(); without this the fixed NOW-based
    # fixtures drift past the real wall clock and the test becomes a date-bomb.
    monkeypatch.setattr(users_router, "utcnow", lambda: NOW)

    db = _ActionDB(_UserResult(user), _SubsResult([sub]))
    result = await user_action(
        13, UserActionIn(action="unban"), db, SimpleNamespace(id=7), SimpleNamespace()
    )

    assert user.is_banned is False
    assert sub.access_revoked_at == NOW - timedelta(days=2)  # unchanged
    assert sub.invite_link == "old-ban-link"  # no fresh invite
    assert create_calls == []
    assert result["access_restored"] == 0
