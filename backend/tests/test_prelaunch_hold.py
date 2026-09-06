"""GK-443: while the hold is on, the bot answers one sentence and sells nothing.

GK-438 stopped the bot answering in groups. In private it still pitched a paid
subscription that cannot be bought yet — about forty people reached it that way
in the four hours before it was stopped on 10 August. This is the mode that
lets the bot come back before it has anything to sell.

The tests are grouped by what would actually go wrong:

* the hold really is the *whole* bot, not a handler that happens to run first;
* it does not undo GK-438 — the practice chat still gets silence;
* nothing member-facing escapes through the scheduler while it is on;
* and with the flag off, none of it exists.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot import handlers, tasks
from app.bot.handlers.hold import HOLD_MESSAGE
from app.config import Settings
from app.config_audit import MUST_BE_DECLARED, _flag_names, audit_configuration, settings_env_names

BOT_ID = 42
PRIVATE_CHAT = 500
#: The production practice chat from the 10 August incident. Hard-coded rather
#: than parameterised: this is the specific room that must stay quiet.
PRACTICE_CHAT = -1002368292795

#: Everything a member can send that used to produce a different answer each
#: time — the greeting, the commands, and the reply-keyboard buttons Grant
#: decided to leave planted on their clients (GK-438).
MEMBER_INPUTS = [
    "/start",
    "/subscribe",
    "/archive",
    "/support",
    "💎 Подписка",
    "🎬 Открыть архив",
    "💬 Поддержка",
    "ℹ️ О сообществе",
    "привет, а когда откроется?",
]


def _build_hold_dispatcher() -> Dispatcher:
    """One dispatcher for the module, built exactly the way production builds it.

    `setup_handlers()` is not re-entrant — the module-level routers attach to a
    parent once and forever — so this runs a single time per test session. With
    the hold on it attaches only `hold.router`, which is itself the property
    under test, so it also cannot collide with the dispatcher
    `test_bot_fallback.py` builds with the flag off.
    """
    original = handlers.settings
    handlers.settings = SimpleNamespace(
        enable_prelaunch_hold=True,
        # GK-446: empty is the shipped default and the state this module tests —
        # the hold covering everyone. The allowlist's own behaviour is in
        # `test_prelaunch_hold_allowlist.py`, which cannot build a second real
        # dispatcher (the handler routers are module-level singletons and attach
        # to a parent once per process) and so exercises the gate directly.
        prelaunch_hold_allowlist_ids=frozenset(),
    )
    try:
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(handlers.setup_handlers())
        return dp
    finally:
        handlers.settings = original


DP = _build_hold_dispatcher()


def _mock_bot() -> AsyncMock:
    bot = AsyncMock(spec=Bot)
    bot.id = BOT_ID
    return bot


def _message_update(text: str, chat_id: int = PRIVATE_CHAT, chat_type: str = "private") -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=TgUser(id=100, is_bot=False, first_name="Test"),
            text=text,
        ),
    )


def _callback_update(
    chat_id: int | None = PRIVATE_CHAT,
    chat_type: str = "private",
    data: str = "buy_start",
) -> Update:
    message = (
        None
        if chat_id is None
        else Message(
            message_id=7,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
        )
    )
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb-1",
            from_user=TgUser(id=100, is_bot=False, first_name="Test"),
            chat_instance="ci-1",
            data=data,
            message=message,
        ),
    )


# ---------------------------------------------------------------------------
# one message, one meaning, no branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", MEMBER_INPUTS)
async def test_every_private_input_produces_the_one_approved_message(text):
    bot = _mock_bot()

    await DP.feed_update(bot, _message_update(text))

    assert bot.await_count == 1, "the hold must answer once, not once per handler"
    sent = bot.await_args.args[0]
    assert sent.text == HOLD_MESSAGE


@pytest.mark.asyncio
async def test_the_hold_never_plants_another_keyboard():
    """Grant decided the menu already on members' clients stays (GK-438). That
    is not permission to hand it to anyone else."""
    bot = _mock_bot()

    await DP.feed_update(bot, _message_update("/start"))

    assert getattr(bot.await_args.args[0], "reply_markup", None) is None


@pytest.mark.asyncio
async def test_an_in_flight_flow_is_cleared_rather_than_left_in_redis():
    """A BuyFlow state frozen by the hold would swallow the member's first free
    text after the hold lifts — GK-350, arriving weeks later with no cause in
    sight."""
    bot = _mock_bot()
    key = StorageKey(bot_id=BOT_ID, chat_id=PRIVATE_CHAT, user_id=100)
    await DP.storage.set_state(key, "BuyFlow:choosing_plan")

    await DP.feed_update(bot, _message_update("1 месяц"))

    assert await DP.storage.get_state(key) is None


@pytest.mark.asyncio
async def test_a_stale_inline_button_gets_the_spinner_stopped_and_the_message():
    """Inline keyboards the bot posted before 10 August are still tappable."""
    bot = _mock_bot()

    await DP.feed_update(bot, _callback_update())

    # `cb.answer()` goes through `bot(...)`; the text goes through send_message.
    assert bot.await_count == 1, "the callback must be answered or the button spins (GK-421)"
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[1] == HOLD_MESSAGE


@pytest.mark.asyncio
async def test_a_callback_we_cannot_place_only_stops_the_spinner():
    """No message means no chat we can prove is private, and guessing a DM can
    fail or, worse, land somewhere public. Stop the spinner, say nothing."""
    bot = _mock_bot()

    await DP.feed_update(bot, _callback_update(chat_id=None))

    assert bot.await_count == 1
    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# it must not undo GK-438
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/start", "🎬 Открыть архив", "привет"])
async def test_the_practice_chat_still_gets_silence(text):
    """The bot is an administrator there again, so it receives every message in
    the room. A hold that answers all of them is the 10 August incident with
    new wording."""
    bot = _mock_bot()

    result = await DP.feed_update(bot, _message_update(text, PRACTICE_CHAT, "supergroup"))

    assert result is UNHANDLED
    assert bot.await_count == 0


@pytest.mark.asyncio
async def test_an_inline_button_left_in_the_practice_chat_stays_dead():
    bot = _mock_bot()

    result = await DP.feed_update(bot, _callback_update(PRACTICE_CHAT, "supergroup"))

    assert result is UNHANDLED
    assert bot.await_count == 0


# ---------------------------------------------------------------------------
# the guarantee is structural, not first-past-the-post
# ---------------------------------------------------------------------------


def test_the_hold_is_the_entire_bot():
    """`setup_handlers()` returns after the hold router, so the handlers that
    create checkouts, issue portal links and open support flows are not in the
    dispatcher at all. "No checkout is created by any path" is then a fact
    about what exists, not a claim about filter ordering."""
    root = DP.sub_routers[0]

    assert root.name == "root"
    assert [r.name for r in root.sub_routers] == ["prelaunch_hold"]


# ---------------------------------------------------------------------------
# the copy — Grant's, and the one thing he asked us to leave out
# ---------------------------------------------------------------------------


def test_support_stays_reachable_inside_the_one_message():
    """A bot that answers everything with a wall is worse than a stopped bot."""
    assert "@GKcurators" in HOLD_MESSAGE


def test_the_hold_message_does_not_sell():
    """Grant, 12.08: «Пришли мне текст на утверждение до включения, без слова
    про "оформите подписку".»"""
    lowered = HOLD_MESSAGE.lower()

    assert "подписк" not in lowered
    assert "оформите" not in lowered
    assert "оплат" not in lowered


# ---------------------------------------------------------------------------
# the flag
# ---------------------------------------------------------------------------


def test_the_hold_is_off_by_default():
    """A hold that defaults to on is a trap for every future deployment, and a
    deployment that silently holds looks exactly like a broken one."""
    assert Settings(_env_file=None).enable_prelaunch_hold is False


def test_the_flag_cannot_silently_disappear_from_an_environment():
    """GK-436's guard covers it by construction — every `ENABLE_*` setting is
    must-declare — which is better than a hand-maintained entry that a rename
    would leave behind. This test is what makes that inheritance deliberate."""
    known = settings_env_names()

    assert "ENABLE_PRELAUNCH_HOLD" in known
    assert "ENABLE_PRELAUNCH_HOLD" in (MUST_BE_DECLARED | _flag_names(known))

    findings = audit_configuration(
        Settings(_env_file=None), environ={}, dotenv_path="/nonexistent/.env"
    )
    assert any(f.env == "ENABLE_PRELAUNCH_HOLD" and f.code == "undeclared" for f in findings)


# ---------------------------------------------------------------------------
# the scheduler — the half that is not an "answer"
# ---------------------------------------------------------------------------


@pytest.fixture
def hold(monkeypatch):
    def _apply(on: bool):
        monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(enable_prelaunch_hold=on))

    return _apply


def _exploding_session():
    def _open():
        raise AssertionError("the job ran: it opened a database session while the hold was on")

    return _open


@pytest.mark.asyncio
async def test_the_hourly_kick_does_not_run_while_the_hold_is_on(hold, monkeypatch):
    """This is the loudest thing the bot does to a member: ban+unban out of the
    channel, then a DM ending «Чтобы вернуться — /subscribe». Left running, the
    hold would be undone at the top of every hour."""
    hold(True)
    monkeypatch.setattr(tasks, "async_session", _exploding_session())
    bot = AsyncMock(spec=Bot)

    await tasks.kick_expired_job(bot)

    assert bot.await_count == 0
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_renewal_reminder_does_not_run_while_the_hold_is_on(hold, monkeypatch):
    hold(True)
    monkeypatch.setattr(tasks, "async_session", _exploding_session())
    notified = AsyncMock()
    monkeypatch.setattr(tasks, "notify_usdt_expiring", notified)

    await tasks.remind_expiring_job()

    notified.assert_not_awaited()


@pytest.mark.asyncio
async def test_with_the_hold_off_the_reminder_runs_as_before(hold, monkeypatch):
    """The guard must not leak into normal operation — this is the assertion
    that would fail if the flag were ever read inverted."""
    hold(False)
    opened = []

    class SessionContext:
        async def __aenter__(self):
            opened.append(True)
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(tasks, "async_session", lambda: SessionContext())

    async def fake_expiring(_session, within_days):
        return []

    monkeypatch.setattr(tasks, "expiring_manual_usdt", fake_expiring)

    await tasks.remind_expiring_job()

    assert opened == [True]
