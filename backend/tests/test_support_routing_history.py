"""GK-378: Telegram support routing + user-linked transcript.

Covers persistence-before-forwarding, curator routing success/failure (without
losing the persisted ticket), admin replies with delivery state, conversation
grouping, history ordering/pagination, and per-user isolation. Fully mocked —
no DB/Redis, matching the rest of the suite.
"""
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramAPIError
from fastapi import HTTPException

from app.api.routers import support as support_api
from app.bot.handlers import support as support_bot

NOW = datetime(2026, 6, 20, tzinfo=UTC)


class FakeTelegramError(TelegramAPIError):
    """Construct without aiogram's TelegramMethod-typed __init__; only `message`
    and `label` are needed for the handler's logging path."""

    def __init__(self, message: str = "boom"):
        Exception.__init__(self, message)
        self.message = message
        self.method = None


class Result:
    def __init__(self, value):
        self._value = value

    def all(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, *results):
        self._results = list(results)
        self.added = []
        self.flushes = 0
        self.commits = 0
        self.events = []
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        assert self._results, "FakeSession.execute called with no queued result"
        return Result(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        self.events.append("flush")
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 900 + self.flushes
            if getattr(obj, "created_at", None) is None:
                obj.created_at = NOW

    async def commit(self):
        self.commits += 1
        self.events.append("commit")


def user(**overrides):
    data = {"id": 7, "tg_id": 70, "username": "x", "first_name": "X"}
    data.update(overrides)
    return SimpleNamespace(**data)


# ── Bot: capture + routing ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_commits_before_routing():
    message = AsyncMock()
    message.text = "  у меня вопрос  "
    session = FakeSession()

    msg = await support_bot._capture(message, session, user())

    assert msg is not None
    assert session.added == [msg]
    # The INSERT is committed, not merely flushed, before routing can start.
    assert session.flushes == 1
    assert session.commits == 1
    assert session.events == ["flush", "commit"]
    assert msg.role == "user"
    assert msg.content == "у меня вопрос"
    assert msg.id is not None


@pytest.mark.asyncio
async def test_capture_ignores_empty_text():
    message = AsyncMock()
    message.text = "   "
    session = FakeSession()

    assert await support_bot._capture(message, session, user()) is None
    assert session.added == []
    assert session.flushes == 0
    assert session.commits == 0


@pytest.mark.asyncio
async def test_route_skipped_when_no_curator_chat(monkeypatch):
    monkeypatch.setattr(support_bot, "settings", SimpleNamespace(support_routing_chat_id=""))
    bot = AsyncMock()
    msg = SimpleNamespace(id=5, content="hi", delivery_status=None)

    status = await support_bot.route_support_message(bot, user(), msg)

    assert status == "skipped"
    assert msg.delivery_status == "skipped"
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_route_forwards_to_numeric_chat_id_and_escapes_content(monkeypatch):
    monkeypatch.setattr(
        support_bot, "settings", SimpleNamespace(support_routing_chat_id="-1001234567890")
    )
    bot = AsyncMock()
    msg = SimpleNamespace(id=5, content="<b>hi</b>", delivery_status=None)

    status = await support_bot.route_support_message(bot, user(username="alice"), msg)

    assert status == "routed"
    assert msg.delivery_status == "routed"
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -1001234567890  # numeric target coerced to int
    assert "@alice" in kwargs["text"]
    assert "&lt;b&gt;hi&lt;/b&gt;" in kwargs["text"]  # HTML-escaped


@pytest.mark.asyncio
async def test_route_username_target_stays_string(monkeypatch):
    monkeypatch.setattr(
        support_bot, "settings", SimpleNamespace(support_routing_chat_id="@gk_support")
    )
    bot = AsyncMock()
    msg = SimpleNamespace(id=5, content="hi", delivery_status=None)

    await support_bot.route_support_message(bot, user(username=None), msg)

    assert bot.send_message.await_args.kwargs["chat_id"] == "@gk_support"


@pytest.mark.asyncio
async def test_route_failure_marks_failed_without_raising(monkeypatch, caplog):
    monkeypatch.setattr(support_bot, "settings", SimpleNamespace(support_routing_chat_id="-100999"))
    bot = AsyncMock()
    bot.send_message.side_effect = FakeTelegramError("forbidden")
    msg = SimpleNamespace(id=5, content="hi", delivery_status=None)

    with caplog.at_level(logging.WARNING):
        status = await support_bot.route_support_message(bot, user(), msg)

    assert status == "failed"
    assert msg.delivery_status == "failed"  # ticket preserved, just flagged
    assert any("routing" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_support_message_handler_persists_then_acknowledges(monkeypatch):
    monkeypatch.setattr(support_bot, "settings", SimpleNamespace(support_routing_chat_id=""))
    message = AsyncMock()
    message.text = "вопрос"
    session = FakeSession()

    await support_bot.support_message(message, session, user())

    assert len(session.added) == 1
    assert session.added[0].delivery_status == "skipped"
    assert session.commits == 2  # durable ticket, then routing status
    assert support_bot.RECEIVED in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_support_message_commit_happens_before_forward(monkeypatch):
    monkeypatch.setattr(
        support_bot, "settings", SimpleNamespace(support_routing_chat_id="-1001234567890")
    )
    message = AsyncMock()
    message.text = "question"
    session = FakeSession()

    async def assert_ticket_is_committed(**_kwargs):
        assert session.events == ["flush", "commit"]

    message.bot.send_message.side_effect = assert_ticket_is_committed

    await support_bot.support_message(message, session, user())

    assert session.added[0].delivery_status == "routed"
    assert session.commits == 2


@pytest.mark.asyncio
async def test_support_fallback_persists_and_enters_chat_state(monkeypatch):
    monkeypatch.setattr(support_bot, "settings", SimpleNamespace(support_routing_chat_id=""))
    message = AsyncMock()
    message.text = "случайный текст"
    session = FakeSession()
    state = AsyncMock()

    await support_bot.support_fallback(message, session, user(), state)

    assert len(session.added) == 1
    assert session.commits == 2
    state.set_state.assert_awaited_once_with(support_bot.SupportFlow.chatting)
    assert support_bot.RECEIVED in message.answer.await_args.args[0]


def test_resolve_chat_id_int_vs_username():
    assert support_bot._resolve_chat_id("-1001234567890") == -1001234567890
    assert support_bot._resolve_chat_id("  -100  ") == -100
    assert support_bot._resolve_chat_id("@gk_support") == "@gk_support"


# ── API: conversations, messages, reply ────────────────────────────────────


@pytest.mark.asyncio
async def test_list_messages_returns_rows_with_delivery_status():
    rows = [
        (SimpleNamespace(id=2, role="assistant", content="reply", delivery_status="delivered", created_at=NOW),
         SimpleNamespace(id=7, username="x", first_name="X")),
        (SimpleNamespace(id=1, role="user", content="q", delivery_status="routed", created_at=NOW),
         SimpleNamespace(id=7, username="x", first_name="X")),
    ]
    db = FakeSession(rows)

    out = await support_api.list_messages(db, object(), user_id=7, limit=100, offset=0)

    assert [m.id for m in out] == [2, 1]  # newest-first, as the DB returns
    assert out[0].delivery_status == "delivered"
    assert out[1].role == "user" and out[1].delivery_status == "routed"
    assert out[0].first_name == "X"


@pytest.mark.asyncio
async def test_list_messages_filters_by_user_for_isolation():
    db = FakeSession([])
    await support_api.list_messages(db, object(), user_id=7, limit=100, offset=0)
    sql = " ".join(str(db.queries[0]).split())
    assert "WHERE support_messages.user_id" in sql


@pytest.mark.asyncio
async def test_list_messages_without_user_has_no_filter():
    db = FakeSession([])
    await support_api.list_messages(db, object(), user_id=None, limit=100, offset=0)
    sql = " ".join(str(db.queries[0]).split())
    assert "WHERE" not in sql


@pytest.mark.asyncio
async def test_list_conversations_groups_and_flags_unanswered():
    last = SimpleNamespace(id=9, role="user", content="latest", delivery_status="skipped", created_at=NOW)
    u = SimpleNamespace(id=7, username="x", first_name="X")
    db = FakeSession(3, [(last, u, 4)])  # total=3, one conversation, 4 messages

    page = await support_api.list_conversations(db, object(), limit=50, offset=0)

    assert page.total == 3
    assert len(page.items) == 1
    c = page.items[0]
    assert c.user_id == 7
    assert c.message_count == 4
    assert c.unanswered is True
    assert c.last_message == "latest"
    assert c.last_role == "user"


@pytest.mark.asyncio
async def test_list_conversations_answered_when_last_is_assistant():
    last = SimpleNamespace(id=9, role="assistant", content="done", delivery_status="delivered", created_at=NOW)
    u = SimpleNamespace(id=7, username="x", first_name="X")
    db = FakeSession(1, [(last, u, 2)])

    page = await support_api.list_conversations(db, object(), limit=50, offset=0)

    assert page.items[0].unanswered is False
    assert page.items[0].last_delivery_status == "delivered"


@pytest.mark.asyncio
async def test_reply_marks_delivered_and_persists(monkeypatch):
    db = FakeSession(user())
    sent = []
    audited = []

    async def fake_send(tg_id, text, reply_markup=None):
        assert db.commits == 1  # transcript is durable before Telegram delivery
        sent.append((tg_id, text))
        return True

    async def fake_audit(_db, **kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(support_api, "send_message", fake_send)
    monkeypatch.setattr(support_api, "audit_record", fake_audit)

    out = await support_api.reply_to_user(
        support_api.ReplyIn(user_id=7, content="hello <b>"),
        db,
        SimpleNamespace(id=1),
        SimpleNamespace(),
    )

    assert out.delivered is True
    assert out.delivery_status == "delivered"
    assert db.added[0].role == "assistant"
    assert db.added[0].delivery_status == "delivered"
    assert db.commits == 2  # transcript before send, then delivery state + audit
    assert sent[0][0] == 70  # delivered to this user's tg_id only
    assert "&lt;b&gt;" in sent[0][1]  # escaped before send
    assert audited[0]["details"]["delivered"] is True


@pytest.mark.asyncio
async def test_reply_marks_failed_when_delivery_fails(monkeypatch):
    db = FakeSession(user())

    async def fake_send(*_a, **_k):
        return False

    async def fake_audit(_db, **_kwargs):
        return None

    monkeypatch.setattr(support_api, "send_message", fake_send)
    monkeypatch.setattr(support_api, "audit_record", fake_audit)

    out = await support_api.reply_to_user(
        support_api.ReplyIn(user_id=7, content="hi"),
        db,
        SimpleNamespace(id=1),
        SimpleNamespace(),
    )

    assert out.delivered is False
    assert out.delivery_status == "failed"
    assert db.added[0].delivery_status == "failed"
    assert db.commits == 2


@pytest.mark.asyncio
async def test_reply_404_for_unknown_user(monkeypatch):
    db = FakeSession(None)
    monkeypatch.setattr(support_api, "send_message", AsyncMock())

    with pytest.raises(HTTPException) as exc:
        await support_api.reply_to_user(
            support_api.ReplyIn(user_id=99, content="x"),
            db,
            SimpleNamespace(id=1),
            SimpleNamespace(),
        )

    assert exc.value.status_code == 404
