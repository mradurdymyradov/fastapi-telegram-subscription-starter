from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import CreateChatInviteLink

from app.config import Settings
from app.services import channel_access
from app.services.channel_access import KickResourceResult


def access_settings(**overrides):
    data = {
        "private_channel_id": -1001,
        "private_channel_invite_link": "",
        "practice_chat_id": -1002,
        "practice_chat_invite_link": "",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_create_invite_links_targets_community_channel_and_practice_chat(monkeypatch):
    calls = []

    class FakeBot:
        async def create_chat_invite_link(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(invite_link=f"https://t.me/+{abs(kwargs['chat_id'])}")

    monkeypatch.setattr(channel_access, "settings", access_settings())

    result = await channel_access.create_invite_links(FakeBot(), name="membership_saas pay#77")

    assert result.all_success is True
    assert result.storage_text == (
        "Community channel: https://t.me/+1001\n"
        "Practice chat: https://t.me/+1002"
    )
    assert [call["chat_id"] for call in calls] == [-1001, -1002]
    assert [call["member_limit"] for call in calls] == [1, 1]


@pytest.mark.asyncio
async def test_invite_links_stop_being_redeemable_after_thirty_days(monkeypatch):
    # GK-434.1: member_limit=1 makes an invite single-use, not short-lived. An
    # invite issued to somebody who never joined stayed valid forever, so a
    # forwarded link was still a way into the paid channel months later.
    calls = []

    class FakeBot:
        async def create_chat_invite_link(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(invite_link="https://t.me/+x")

    monkeypatch.setattr(channel_access, "settings", access_settings())

    before = datetime.now(UTC)
    await channel_access.create_invite_links(FakeBot(), name="pay#1")
    after = datetime.now(UTC)

    assert len(calls) == 2
    for call in calls:
        expires = call["expire_date"]
        assert before + timedelta(days=30) <= expires <= after + timedelta(days=30)
        # Still exactly one join per link — the expiry is added, not swapped in.
        assert call["member_limit"] == 1


@pytest.mark.asyncio
async def test_create_invite_links_never_returns_configured_fallback_without_chat_id(monkeypatch):
    # A resource configured with only a static shared fallback link (no chat_id)
    # must NOT hand that link to a subscriber — it is unrevocable. Issue nothing
    # for it and surface an error so fulfillment pauses for an admin (GK-406 AC1/AC2).
    class FakeBot:
        async def create_chat_invite_link(self, **kwargs):
            return SimpleNamespace(invite_link=f"https://t.me/+{abs(kwargs['chat_id'])}")

    monkeypatch.setattr(
        channel_access,
        "settings",
        access_settings(
            private_channel_id=0,
            private_channel_invite_link="https://t.me/+shared",
            practice_chat_id=-1002,
        ),
    )

    result = await channel_access.create_invite_links(FakeBot(), name="pay#1")

    assert result.all_success is False
    community = next(r for r in result.results if r.resource.key == "community_channel")
    assert community.success is False
    assert community.invite_link is None
    assert "https://t.me/+shared" not in (result.storage_text or "")


@pytest.mark.asyncio
async def test_create_invite_links_never_falls_back_when_creation_fails(monkeypatch):
    # When one-time link creation fails, we must NOT substitute the configured
    # shared fallback link — surface the error instead (GK-406 AC1/AC2).
    class FakeBot:
        async def create_chat_invite_link(self, **kwargs):
            raise TelegramForbiddenError(
                method=CreateChatInviteLink(chat_id=kwargs["chat_id"]),
                message="bot is not an administrator of the chat",
            )

    monkeypatch.setattr(
        channel_access,
        "settings",
        access_settings(
            private_channel_invite_link="https://t.me/+shared",
            practice_chat_id=0,
        ),
    )

    result = await channel_access.create_invite_links(FakeBot(), name="pay#1")

    assert result.all_success is False
    community = next(r for r in result.results if r.resource.key == "community_channel")
    assert community.success is False
    assert community.invite_link is None
    assert "https://t.me/+shared" not in (result.storage_text or "")


@pytest.mark.asyncio
async def test_revoke_invite_links_targets_stored_community_and_practice_links(monkeypatch):
    calls = []

    class FakeBot:
        async def revoke_chat_invite_link(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(channel_access, "settings", access_settings())

    result = await channel_access.revoke_invite_links(
        FakeBot(),
        "Community channel: https://t.me/+channel\n"
        "Practice chat: https://t.me/+practice",
    )

    assert result.success is True
    assert calls == [
        {"chat_id": -1001, "invite_link": "https://t.me/+channel"},
        {"chat_id": -1002, "invite_link": "https://t.me/+practice"},
    ]


@pytest.mark.asyncio
async def test_revoke_invite_links_targets_bare_single_resource_link(monkeypatch):
    calls = []

    class FakeBot:
        async def revoke_chat_invite_link(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(channel_access, "settings", access_settings(practice_chat_id=0))

    result = await channel_access.revoke_invite_links(
        FakeBot(),
        "https://t.me/+single",
    )

    assert result.success is True
    assert calls == [{"chat_id": -1001, "invite_link": "https://t.me/+single"}]


@pytest.mark.asyncio
async def test_revoke_invite_links_empty_targets_are_not_success(monkeypatch):
    monkeypatch.setattr(channel_access, "settings", access_settings())

    result = await channel_access.revoke_invite_links(
        SimpleNamespace(),
        "not an invite link",
    )

    assert result.success is False
    assert result.error == "no invite links parsed"


@pytest.mark.asyncio
async def test_revoke_invite_links_fails_on_unrevocable_fallback_link(monkeypatch):
    # A stored link that equals the configured shared fallback is a channel-wide
    # credential the bot cannot revoke per-user. Revocation MUST report failure,
    # not a silent success, so a kick never "completes" while the former member
    # keeps a working link (GK-406 AC3).
    class FakeBot:
        async def revoke_chat_invite_link(self, **_kwargs):
            raise AssertionError("unrevocable shared fallback link must not be reported revoked")

    monkeypatch.setattr(
        channel_access,
        "settings",
        access_settings(
            private_channel_invite_link="https://t.me/+shared",
            practice_chat_id=0,
        ),
    )

    result = await channel_access.revoke_invite_links(
        FakeBot(),
        "https://t.me/+shared",
    )

    assert result.success is False
    assert result.results[0].resource.key == "community_channel"
    assert result.results[0].success is False
    assert "unrevocable" in (result.results[0].error or "")


@pytest.mark.asyncio
async def test_revoke_invite_links_reports_unmapped_storage_line(monkeypatch):
    monkeypatch.setattr(channel_access, "settings", access_settings())

    result = await channel_access.revoke_invite_links(
        SimpleNamespace(),
        "Unknown: https://t.me/+stale",
    )

    assert result.success is False
    assert "could not map invite link" in result.error


@pytest.mark.asyncio
async def test_kick_user_revokes_all_configured_resources(monkeypatch):
    calls = []

    class FakeBot:
        async def ban_chat_member(self, **kwargs):
            calls.append(("ban", kwargs["chat_id"], kwargs["user_id"]))

        async def unban_chat_member(self, **kwargs):
            calls.append(("unban", kwargs["chat_id"], kwargs["user_id"]))

    monkeypatch.setattr(channel_access, "settings", access_settings())

    result = await channel_access.kick_user(FakeBot(), tg_id=7007)

    assert result.success is True
    assert result.retry_after is None
    assert [(kind, chat_id) for kind, chat_id, _user_id in calls] == [
        ("ban", -1001),
        ("unban", -1001),
        ("ban", -1002),
        ("unban", -1002),
    ]


@pytest.mark.asyncio
async def test_kick_user_keeps_per_resource_retry_details(monkeypatch):
    monkeypatch.setattr(channel_access, "settings", access_settings())

    async def fake_kick(_bot, _tg_id, resource):
        if resource.key == "community_channel":
            return KickResourceResult(resource, True)
        return KickResourceResult(
            resource,
            False,
            retry_after=42,
            error="retry_after=42",
        )

    monkeypatch.setattr(channel_access, "_kick_user_from_resource", fake_kick)

    result = await channel_access.kick_user(SimpleNamespace(), tg_id=7007)

    assert result.success is False
    assert result.retry_after == 42
    assert "practice_chat: retry_after=42" in result.error
    assert [(r.resource.key, r.success) for r in result.resource_results] == [
        ("community_channel", True),
        ("practice_chat", False),
    ]


def test_prod_security_requires_two_distinct_telegram_resources():
    settings = Settings(
        _env_file=None,
        app_env="prod",
        jwt_secret="x" * 32,
        admin_default_password="strong-password",
        private_channel_id=-1001,
        practice_chat_id=0,
    )

    assert "PRACTICE_CHAT_ID is required in production" in settings.validate_security()

    duplicate = Settings(
        _env_file=None,
        app_env="prod",
        jwt_secret="x" * 32,
        admin_default_password="strong-password",
        private_channel_id=-1001,
        practice_chat_id=-1001,
    )

    assert "PRIVATE_CHANNEL_ID and PRACTICE_CHAT_ID must be different" in duplicate.validate_security()
