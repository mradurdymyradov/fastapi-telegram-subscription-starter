"""GK-460: lifting the hold and arming removals are two decisions, not one.

`kick_expired_job` ban+unbans expired members out of every configured Telegram
resource, hourly and unattended. Until this task the only thing standing in
front of it was `ENABLE_PRELAUNCH_HOLD` — so the single `.env` edit that returns
the bot to selling on launch day also armed removals against whatever
`PRIVATE_CHANNEL_ID` and `PRACTICE_CHAT_ID` happened to point at, within the
hour, with nothing anywhere naming the intended target.

The contrast was already in this repo: `python -m app.ops.cutover` removes the
same people from the same rooms and refuses without an exact confirmation token
**and** an `--expect-chats` naming every resource it reaches. These tests hold the
scheduled job to the same standard, and pin the three decisions that are easy to
quietly lose later:

* the flag is off by default, and off is silent — no alert for the intended state;
* armed-but-unbound refuses **and** says so out loud, because an operator who set
  the flag believes removals are running;
* the ban-retry net is deliberately *not* behind this gate.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.bot import tasks
from app.config import Settings, parse_tg_chat_id_list
from app.config_audit import MUST_BE_DECLARED, _flag_names, audit_configuration, settings_env_names
from app.services import subscription as subscription_service
from app.services.channel_access import SubscriptionAccessRevokeResult

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

#: The two production rooms, as measured on 2026-08-23. Used as the "configured"
#: pair throughout so a mismatch in these tests reads like a real one.
CHANNEL = -1001945266701
PRACTICE = -1002368292795


def job_settings(**overrides):
    """What `kick_expired_job` reads, with removals armed and bound correctly."""
    data = {
        "enable_prelaunch_hold": False,
        "enable_expiry_removals": True,
        "expiry_removals_expect_chat_ids": frozenset({CHANNEL, PRACTICE}),
        "private_channel_id": CHANNEL,
        "practice_chat_id": PRACTICE,
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
        "expires_at": NOW - timedelta(days=1),
        "grace_started_at": None,
        "grace_ends_at": None,
        "cancel_at_period_end": False,
        "is_comp": False,
        "access_revoke_attempted_at": None,
        "access_revoked_at": None,
        "access_revoke_abandoned_at": None,
        "access_revoke_retry_after_at": None,
        "access_revoke_attempts": 0,
        "access_revoke_error": None,
        "invite_link": "https://t.me/+stored",
        "user": SimpleNamespace(tg_id=1001),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def refresh(self, *_args):  # pragma: no cover - users are pre-loaded here
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


def install_job(monkeypatch, *, settings, due=(), banned=()):
    """Wire the job to in-memory rows, a scripted Telegram and captured alerts."""
    context = SessionContext()
    state = SimpleNamespace(expire_calls=0, revoked=[], messages=[], alerts=[])

    async def fake_expire(_session):
        state.expire_calls += 1
        return list(due)

    async def fake_pending(_session):
        return list(banned)

    async def fake_revoke(_bot, tg_id, invite_link):
        state.revoked.append((tg_id, invite_link))
        return SubscriptionAccessRevokeResult(True)

    async def fake_alert(text, *, key=None, rate_limit_seconds=300, severity="warn"):
        state.alerts.append({"text": text, "key": key, "severity": severity})
        return True

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "async_session", lambda: context)
    monkeypatch.setattr(tasks, "expire_subscriptions", fake_expire)
    monkeypatch.setattr(tasks, "subscriptions_pending_ban_revoke", fake_pending)
    monkeypatch.setattr(tasks, "revoke_subscription_access", fake_revoke)
    monkeypatch.setattr(tasks, "send_ops_alert", fake_alert)
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)
    return state


def fake_bot(state):
    async def send_message(tg_id, text, **_kwargs):
        state.messages.append((tg_id, text))

    return SimpleNamespace(send_message=send_message)


# ---------------------------------------------------------------------------
# the flag itself
# ---------------------------------------------------------------------------


def test_arming_is_off_by_default():
    """Off by omission and off by decision must both mean off. A removal
    control that defaults to armed is one `.env` file away from a surprise."""
    settings = Settings(_env_file=None)

    assert settings.enable_expiry_removals is False
    assert settings.expiry_removals_expect_chat_ids == frozenset()


def test_the_flag_cannot_silently_disappear_from_an_environment():
    """Inherited from GK-436 by construction — every `ENABLE_*` key is
    must-declare — which is what makes this a property of the guard rather than
    a hand-maintained entry a rename would strand."""
    known = settings_env_names()

    assert "ENABLE_EXPIRY_REMOVALS" in known
    assert "ENABLE_EXPIRY_REMOVALS" in (MUST_BE_DECLARED | _flag_names(known))


def test_arming_without_naming_the_chats_is_a_startup_error():
    """The worst of the three states, caught before the first hour: the
    operator believes removals run, and nothing removes anybody."""
    findings = audit_configuration(
        Settings(_env_file=None, enable_expiry_removals=True),
        environ={"ENABLE_EXPIRY_REMOVALS": "true"},
        dotenv_path="/nonexistent/.env",
    )

    assert any(
        f.env == "EXPIRY_REMOVALS_EXPECT_CHATS" and f.severity == "error" for f in findings
    )


def test_a_malformed_binding_refuses_the_boot_rather_than_reading_as_empty():
    errors = Settings(
        _env_file=None,
        jwt_secret="x" * 32,
        expiry_removals_expect_chats="@membership",
    ).validate_security()

    assert any("EXPIRY_REMOVALS_EXPECT_CHATS" in error for error in errors)


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


def test_chat_ids_parse_with_the_whitespace_a_human_leaves():
    assert parse_tg_chat_id_list(f" {CHANNEL} , {PRACTICE} ,") == frozenset({CHANNEL, PRACTICE})


def test_an_unset_id_is_not_a_target():
    """`0` is what both chat settings read as when nothing is configured. Taking
    it as a named target would let "I confirmed the target" be satisfied by
    confirming nothing."""
    with pytest.raises(ValueError):
        parse_tg_chat_id_list("0")


def test_a_username_is_rejected_because_it_is_not_durable():
    with pytest.raises(ValueError):
        parse_tg_chat_id_list("@membership")


def test_an_empty_binding_is_empty_rather_than_an_error():
    assert parse_tg_chat_id_list("  ") == frozenset()


# ---------------------------------------------------------------------------
# the binding
# ---------------------------------------------------------------------------


def test_the_binding_covers_every_chat_a_removal_would_actually_reach():
    """`_removal_target_chat_ids` is a second list of Telegram resources, and a
    second list is a thing that drifts. Adding a third resource to
    `configured_access_resources` without adding it here would leave that room
    outside the confirmation — so this fails on that commit rather than on the
    day somebody is removed from a chat nobody named."""
    from app.services import channel_access

    settings = job_settings()
    original = channel_access.settings
    channel_access.settings = SimpleNamespace(
        private_channel_id=CHANNEL,
        private_channel_invite_link="",
        practice_chat_id=PRACTICE,
        practice_chat_invite_link="",
    )
    try:
        reachable = {
            resource.chat_id
            for resource in channel_access.configured_access_resources()
            if resource.chat_id
        }
    finally:
        channel_access.settings = original

    assert tasks._removal_target_chat_ids(settings) == reachable


def test_a_correct_binding_allows_the_sweep():
    assert tasks._expiry_removals_refusal(job_settings()) is None


def test_the_flag_alone_is_not_enough():
    refusal = tasks._expiry_removals_refusal(
        job_settings(expiry_removals_expect_chat_ids=frozenset())
    )

    assert refusal is not None
    assert "EXPIRY_REMOVALS_EXPECT_CHATS" in refusal
    # The refusal has to carry the answer; a message that only says "wrong"
    # sends the operator to the source at the worst possible moment.
    assert str(CHANNEL) in refusal and str(PRACTICE) in refusal


def test_a_repointed_chat_is_refused_and_both_sides_are_printed():
    """The failure this whole task exists for: the config now names a room
    nobody confirmed."""
    refusal = tasks._expiry_removals_refusal(job_settings(private_channel_id=-100999))

    assert refusal is not None
    assert "-100999" in refusal
    assert str(CHANNEL) in refusal


def test_a_chat_that_appears_without_being_named_is_refused_too():
    """Set equality, not membership. A resource added to the deployment after
    the binding was written is exactly as unconfirmed as a repointed one."""
    refusal = tasks._expiry_removals_refusal(
        job_settings(expiry_removals_expect_chat_ids=frozenset({CHANNEL}))
    )

    assert refusal is not None
    assert str(PRACTICE) in refusal


def test_naming_extra_chats_does_not_arm_them():
    refusal = tasks._expiry_removals_refusal(
        job_settings(expiry_removals_expect_chat_ids=frozenset({CHANNEL, PRACTICE, -100777}))
    )

    assert refusal is not None


def test_an_unconfigured_deployment_removes_nobody():
    refusal = tasks._expiry_removals_refusal(
        job_settings(
            private_channel_id=0,
            practice_chat_id=0,
            expiry_removals_expect_chat_ids=frozenset(),
        )
    )

    assert refusal is not None
    assert "PRIVATE_CHANNEL_ID" in refusal


# ---------------------------------------------------------------------------
# the job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifting_the_hold_no_longer_arms_removals(monkeypatch):
    """The 29.08 sequence, as it will actually be run: `ENABLE_PRELAUNCH_HOLD`
    goes false and nothing else changes. Before GK-460 the next hour removed
    three members — two of them the client's manager (GK-484)."""
    sub = make_sub()
    state = install_job(
        monkeypatch,
        settings=job_settings(enable_expiry_removals=False),
        due=[sub],
    )

    await tasks.kick_expired_job(fake_bot(state))

    assert state.revoked == []
    assert state.messages == []
    assert sub.status == "active"
    assert sub.access_revoked_at is None
    # Not merely "no removal happened": the due set is never even asked for, so
    # there is no partially-walked list to reason about.
    assert state.expire_calls == 0


@pytest.mark.asyncio
async def test_the_intended_off_state_does_not_page_anyone(monkeypatch):
    """Off is the correct state for the whole pre-launch window. An hourly
    alert about a deliberate setting is how an alert channel stops being read."""
    state = install_job(
        monkeypatch,
        settings=job_settings(enable_expiry_removals=False),
        due=[make_sub()],
    )

    await tasks.kick_expired_job(fake_bot(state))

    assert state.alerts == []


@pytest.mark.asyncio
async def test_armed_and_bound_removes_as_before(monkeypatch):
    """The guard must not leak into normal operation — this is the assertion
    that fails if the binding is ever read inverted."""
    sub = make_sub()
    state = install_job(monkeypatch, settings=job_settings(), due=[sub])

    await tasks.kick_expired_job(fake_bot(state))

    assert state.revoked == [(1001, "https://t.me/+stored")]
    assert sub.access_revoked_at == NOW
    assert sub.status == "expired"
    assert state.messages and "/subscribe" in state.messages[0][1]
    assert state.alerts == []


@pytest.mark.asyncio
async def test_armed_at_the_wrong_chat_removes_nobody_and_says_so(monkeypatch):
    """Armed and refused is the one state worth waking someone for: the
    operator believes members are being removed on schedule."""
    sub = make_sub()
    state = install_job(
        monkeypatch,
        settings=job_settings(private_channel_id=-100999),
        due=[sub],
    )

    await tasks.kick_expired_job(fake_bot(state))

    assert state.revoked == []
    assert sub.access_revoked_at is None
    assert len(state.alerts) == 1
    alert = state.alerts[0]
    assert alert["severity"] == "error"
    # Rate-limited on a stable key: hourly, this is one alert a day, not 24.
    assert alert["key"] == "expiry_removals_refused"
    assert "-100999" in alert["text"]


@pytest.mark.asyncio
async def test_a_dead_alert_channel_does_not_take_the_job_down(monkeypatch):
    """The refusal is the safety property; telling someone about it is not
    allowed to become a new way for the hourly job to fail."""
    state = install_job(
        monkeypatch,
        settings=job_settings(private_channel_id=-100999),
        due=[make_sub()],
    )

    async def exploding_alert(*_args, **_kwargs):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(tasks, "send_ops_alert", exploding_alert)

    await tasks.kick_expired_job(fake_bot(state))

    assert state.revoked == []


@pytest.mark.asyncio
async def test_the_ban_retry_net_is_deliberately_outside_this_gate(monkeypatch):
    """GK-400's durable retry converges a removal an administrator already
    ordered by hand, in a panel that attempts the same Telegram call
    immediately and with no gate at all. Holding it here would not prevent a
    removal — it would leave an ordered one silently unenforced."""
    banned = make_sub(id=9, expires_at=NOW + timedelta(days=20), user=SimpleNamespace(tg_id=5005))
    state = install_job(
        monkeypatch,
        settings=job_settings(enable_expiry_removals=False),
        due=[make_sub()],
        banned=[banned],
    )

    await tasks.kick_expired_job(fake_bot(state))

    assert state.revoked == [(5005, "https://t.me/+stored")]
    # A ban does not expire the paid subscription, and sends no "ended" notice.
    assert banned.status == "active"
    assert state.messages == []


@pytest.mark.asyncio
async def test_the_hold_still_wins_over_an_armed_flag(monkeypatch):
    """Arming removals is not a way to reach members during the hold. GK-443's
    guarantee is that nothing member-facing runs; this must not become the
    exception to it."""
    state = install_job(
        monkeypatch,
        settings=job_settings(enable_prelaunch_hold=True),
        due=[make_sub()],
        banned=[make_sub(id=9, user=SimpleNamespace(tg_id=5005))],
    )

    def _exploding_session():
        raise AssertionError("the job ran: it opened a session while the hold was on")

    monkeypatch.setattr(tasks, "async_session", _exploding_session)

    await tasks.kick_expired_job(fake_bot(state))

    assert state.revoked == []
    assert state.alerts == []
