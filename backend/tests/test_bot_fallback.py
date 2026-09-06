"""GK-350: stuck FSM states must not swallow user messages silently.

Covers the fallback router (hint instead of silence in private chats, no
spam in groups), /start clearing any in-flight flow state, and the FSM
storage TTL that lets abandoned flows self-expire.

GK-438 extends the group half: *no* handler — not just the fallback — may
answer in a group or channel, because the bot is an admin in the production
channel and practice chat and therefore receives everything posted there.

GK-453 is the same failure seen from the other end: not a state that swallows
free text into silence, but one that swallows a *button* into the wrong answer.
Its tests live here because this module owns the only real dispatcher the suite
can build — `setup_handlers()` attaches module-level routers to a parent once
per process.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.handlers import (
    FALLBACK_HINT,
    _private_callback,
    gift,
    portal,
    referral,
    setup_handlers,
)
from app.bot.handlers.start import start_basic, start_with_ref
from app.bot.keyboards import MENU_BUTTON_TEXTS, main_menu

BOT_ID = 42

# setup_handlers() is not re-entrant (module-level routers attach to one
# parent forever), so the whole module shares a single dispatcher — same as
# production, which builds it exactly once.
DP = Dispatcher(storage=MemoryStorage())
DP.include_router(setup_handlers())


def _update(chat_id: int, chat_type: str, text: str = "привет") -> Update:
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


def _mock_bot() -> AsyncMock:
    bot = AsyncMock(spec=Bot)
    bot.id = BOT_ID
    return bot


@pytest.mark.asyncio
async def test_free_text_in_stuck_buyflow_state_gets_hint():
    """The exact live failure: BuyFlow:choosing_plan + free text = silence."""
    dp = DP
    bot = _mock_bot()
    key = StorageKey(bot_id=BOT_ID, chat_id=500, user_id=100)
    await dp.storage.set_state(key, "BuyFlow:choosing_plan")

    await dp.feed_update(bot, _update(chat_id=500, chat_type="private"))

    assert bot.await_count == 1
    sent = bot.await_args.args[0]
    assert sent.text == FALLBACK_HINT


@pytest.mark.asyncio
async def test_group_message_in_state_stays_silent():
    """The fallback must never make the bot chat in the practice group."""
    dp = DP
    bot = _mock_bot()
    key = StorageKey(bot_id=BOT_ID, chat_id=-600, user_id=100)
    await dp.storage.set_state(key, "BuyFlow:choosing_plan")

    result = await dp.feed_update(bot, _update(chat_id=-600, chat_type="group"))

    assert result is UNHANDLED
    assert bot.await_count == 0


def _callback_update(chat_id: int, chat_type: str, data: str = "buy_start") -> Update:
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb-1",
            from_user=TgUser(id=100, is_bot=False, first_name="Test"),
            chat_instance="ci-1",
            data=data,
            message=Message(
                message_id=7,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type=chat_type),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_start_in_group_never_plants_the_reply_keyboard():
    """The 2026-08-10 live incident: `/start` in the production practice chat.

    `main_menu()` is not selective, so answering there hands the reply keyboard
    to every member of the group at once.
    """
    bot = _mock_bot()

    result = await DP.feed_update(bot, _update(-1002368292795, "supergroup", "/start"))

    assert result is UNHANDLED
    assert bot.await_count == 0


@pytest.mark.asyncio
async def test_archive_button_in_group_does_not_pitch_a_subscription():
    """What members saw: a tap in the group, a public "buy a subscription" reply."""
    bot = _mock_bot()

    result = await DP.feed_update(
        bot, _update(-1002368292795, "supergroup", "🎬 Открыть архив")
    )

    assert result is UNHANDLED
    assert bot.await_count == 0


@pytest.mark.asyncio
async def test_archive_button_still_works_in_private(monkeypatch):
    """The guard must silence groups only — the private flow is the product."""
    sent = AsyncMock()
    monkeypatch.setattr(portal, "send_archive_link", sent)

    await DP.feed_update(
        _mock_bot(),
        _update(500, "private", "🎬 Открыть архив"),
        session=AsyncMock(),
        user=SimpleNamespace(),
    )

    sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_button_left_in_a_group_is_dead():
    """Keyboards the bot already posted to the group before the fix stay inert."""
    bot = _mock_bot()

    result = await DP.feed_update(bot, _callback_update(-1002368292795, "supergroup"))

    assert result is UNHANDLED
    assert bot.await_count == 0


def test_callback_without_a_message_is_not_dropped():
    """GK-421: a callback we cannot place must not be swallowed into a spinner."""
    assert _private_callback(SimpleNamespace(message=None)) is True


@pytest.mark.asyncio
async def test_start_basic_clears_stuck_state():
    message = AsyncMock()
    state = AsyncMock()

    await start_basic(message=message, user=SimpleNamespace(), state=state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_deep_link_clears_stuck_state():
    message = AsyncMock()
    state = AsyncMock()

    await start_with_ref(
        message=message,
        command=SimpleNamespace(args=None),
        session=AsyncMock(),
        user=SimpleNamespace(),
        state=state,
    )

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()


def test_fallback_router_is_registered_last():
    root = DP.sub_routers[0]
    assert root.name == "root"
    assert root.sub_routers[-1].name == "fallback"


def test_with_the_prelaunch_hold_off_the_product_is_intact():
    """GK-443 short-circuits `setup_handlers()` when the hold is on. This
    dispatcher was built with the flag at its default, so the hold must be
    absent and every real router present — the regression that would turn a
    forgotten `.env` line into a bot that sells nothing on launch day."""
    root = DP.sub_routers[0]
    names = [r.name for r in root.sub_routers]

    assert "prelaunch_hold" not in names
    assert names == [
        "portal",
        "start",
        # GK-486: before `subscription`, and the order is the mechanism — this
        # router claims the buying entry points for accounts that must never be
        # charged. Behind `subscription` it would claim nothing at all.
        "no_charge",
        "subscription",
        "cabinet",
        "referral",
        # GK-453: gift before support, not after — see
        # `test_gift_button_reaches_the_gift_flow_from_the_support_dialog`.
        "gift",
        "support",
        "fallback",
    ]


def test_fsm_storage_expires_abandoned_flows():
    from app.bot import main as bot_main

    storage = bot_main._build_storage()
    assert storage.state_ttl == timedelta(hours=24)
    assert storage.data_ttl == timedelta(hours=24)
# --------------------------------------------------------------------- GK-453
# The reply keyboard is permanent: it sits under the chat and a member can tap
# it from inside any FSM state. Several handlers claim *every* message in their
# state — the support dialog, and the promo / email / tx-hash steps of checkout
# — so a tap whose own handler is registered behind one of them is consumed as
# free text. Grant hit it on 19.08: «🎁 Подарить» answered by the support ticket
# receipt, «периодически», which is precisely how it looks when whether it works
# depends on the state you happen to be in.


def test_menu_button_texts_matches_the_keyboard_it_is_derived_from():
    """The guard is only as good as the set — it must be the keyboard itself."""
    rendered = {button.text for row in main_menu().keyboard for button in row}

    assert MENU_BUTTON_TEXTS == rendered
    assert "🎁 Подарить" in MENU_BUTTON_TEXTS


@pytest.mark.asyncio
async def test_gift_button_reaches_the_gift_flow_from_the_support_dialog(monkeypatch):
    """Grant's screenshot, as a test: tap «🎁 Подарить» while talking to support."""
    shown = AsyncMock()
    monkeypatch.setattr(gift, "_show_gift_plans_message", shown)
    bot = _mock_bot()
    await DP.storage.set_state(
        StorageKey(bot_id=BOT_ID, chat_id=510, user_id=100), "SupportFlow:chatting"
    )

    await DP.feed_update(
        bot,
        _update(510, "private", "🎁 Подарить"),
        session=AsyncMock(),
        user=SimpleNamespace(),
    )

    shown.assert_awaited_once()
    # Nothing else answered — in particular not the support receipt, which is
    # what a member saw instead of the gift screen.
    assert bot.await_count == 0


@pytest.mark.asyncio
async def test_gift_button_reaches_the_gift_flow_with_no_state_at_all(monkeypatch):
    """The commoner case: support's stateless fallback claims all plain text."""
    shown = AsyncMock()
    monkeypatch.setattr(gift, "_show_gift_plans_message", shown)
    bot = _mock_bot()

    await DP.feed_update(
        bot,
        _update(511, "private", "🎁 Подарить"),
        session=AsyncMock(),
        user=SimpleNamespace(),
    )

    shown.assert_awaited_once()
    assert bot.await_count == 0


@pytest.mark.asyncio
async def test_slash_gift_escapes_the_support_dialog(monkeypatch):
    """`/gift` was eaten too — filed as a support ticket reading "/gift".

    The stateless fallback already let `/`-prefixed text through; the dialog's
    own catch-all did not, so this only ever failed for a member mid-conversation
    with a curator. Hence the state.
    """
    shown = AsyncMock()
    monkeypatch.setattr(gift, "_show_gift_plans_message", shown)
    await DP.storage.set_state(
        StorageKey(bot_id=BOT_ID, chat_id=512, user_id=100), "SupportFlow:chatting"
    )

    await DP.feed_update(
        _mock_bot(),
        _update(512, "private", "/gift"),
        session=AsyncMock(),
        user=SimpleNamespace(),
    )

    shown.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_button_in_the_promo_step_is_not_read_as_a_promo_code(monkeypatch):
    """Same trap, different state: mid-checkout the tap became a promo code."""
    monkeypatch.setattr(referral, "leaderboard", AsyncMock(return_value=[]))
    bot = _mock_bot()
    await DP.storage.set_state(
        StorageKey(bot_id=BOT_ID, chat_id=513, user_id=100), "BuyFlow:awaiting_promo"
    )

    await DP.feed_update(bot, _update(513, "private", "🏆 Лидерборд"), session=AsyncMock())

    assert bot.await_count == 1
    assert "Лидерборд партнёров" in bot.await_args.args[0].text


@pytest.mark.asyncio
async def test_free_text_in_the_support_dialog_is_still_a_ticket(monkeypatch):
    """The guard must exclude the labels and nothing else."""
    captured = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.support._capture", captured)
    await DP.storage.set_state(
        StorageKey(bot_id=BOT_ID, chat_id=514, user_id=100), "SupportFlow:chatting"
    )

    await DP.feed_update(
        _mock_bot(),
        _update(514, "private", "Как связаться с Павлом?"),
        session=AsyncMock(),
        user=SimpleNamespace(),
    )

    captured.assert_awaited_once()
