"""GK-446: a named few walk past the pre-launch hold, and nobody else does.

Grant asked to check the finished texts on a live bot before launch. GK-443's
hold is exactly what hides them — with it on, the greeting, the plans, the
checkout and the portal link are not in the dispatcher at all. This is the
mechanism that gives those handlers back to a named few.

That makes it a *hole in a safety mechanism*, so the tests are written from the
failure side. What would actually go wrong:

* an id that is not on the list reaches a real checkout three weeks early;
* an event with no identifiable user is treated as allowed;
* the message observer is gated and the callback observer is forgotten, so an
  inline button from the bot's pre-10-August history walks straight through;
* a typo in `.env` reads as a valid list and admits the wrong person, or reads
  as an empty one and admits nobody while looking correct;
* the mechanism turns itself on when nobody asked.

**One thing here is modelled rather than executed, and it is worth naming.**
The handler routers are module-level singletons: aiogram attaches a router to a
parent once per process, so `setup_handlers()` cannot run twice in a test
session and `test_prelaunch_hold.py` / `test_bot_fallback.py` have already spent
both of its useful configurations. Everything below therefore drives the *real*
`_prelaunch_allowlist_gate` and the *real* `_allowlist_filter`, with stub
children injected through the gate's `include` seam. The composition — gate
first, hold last — is mirrored from `setup_handlers()` rather than taken from
it. The mirror is four lines and it is asserted; the seam is what keeps the
predicate and both filters from being described instead of run.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot import handlers
from app.config import Settings, parse_tg_id_allowlist

BOT_ID = 42
PRIVATE_CHAT = 500
GRANT = 111111111
CURATOR = 222222222
STRANGER = 999999999

#: What the stub children answer. Any of these coming back means the event got
#: past the gate and would, in production, have reached the real bot.
LIVE_ANSWER = "LIVE"
HOLD_ANSWER = "HOLD"


def _stub_member_routers(parent: Router) -> None:
    """Stand-ins for the routers `_include_member_routers` would attach.

    Deliberately catch-all, because the question under test is whether the event
    got *in*, not what it met once inside.
    """
    child = Router(name="stub_member")

    @child.message()
    async def _msg(message: Message) -> None:
        await message.answer(LIVE_ANSWER)

    @child.callback_query()
    async def _cb(callback: CallbackQuery) -> None:
        await callback.answer(LIVE_ANSWER)

    parent.include_router(child)


def _stub_hold_router() -> Router:
    """A stand-in for `hold.router` — the catch-all everyone else lands on."""
    held = Router(name="stub_hold")

    @held.message()
    async def _msg(message: Message) -> None:
        await message.answer(HOLD_ANSWER)

    @held.callback_query()
    async def _cb(callback: CallbackQuery) -> None:
        await callback.answer(HOLD_ANSWER)

    return held


def _dispatcher(allowlist: frozenset[int]) -> Dispatcher:
    """The hold branch of `setup_handlers()`, mirrored with stub children.

    Mirrored, not called — see the module docstring. The two properties being
    reproduced are the ones that carry the safety: the gate is included
    **first**, and `hold.router` is included **last** as the catch-all, so an
    event the gate declines falls onto the заглушка rather than off the end.
    """
    root = Router(name="root")
    root.message.filter(F.chat.type == ChatType.PRIVATE)
    root.callback_query.filter(handlers._private_callback)
    if allowlist:
        root.include_router(
            handlers._prelaunch_allowlist_gate(allowlist, include=_stub_member_routers)
        )
    root.include_router(_stub_hold_router())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(root)
    return dp


def _mock_bot() -> AsyncMock:
    bot = AsyncMock(spec=Bot)
    bot.id = BOT_ID
    return bot


def _message_update(user_id: int | None, chat_type: str = "private") -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=PRIVATE_CHAT, type=chat_type),
            from_user=(
                None if user_id is None else TgUser(id=user_id, is_bot=False, first_name="T")
            ),
            text="/subscribe",
        ),
    )


def _callback_update(user_id: int) -> Update:
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb-1",
            from_user=TgUser(id=user_id, is_bot=False, first_name="T"),
            chat_instance="ci-1",
            data="buy_start",
            message=Message(
                message_id=7,
                date=datetime.now(UTC),
                chat=Chat(id=PRIVATE_CHAT, type="private"),
            ),
        ),
    )


async def _answered(dp: Dispatcher, update: Update) -> str | None:
    """Run one update and report which side answered it, or ``None`` for silence.

    `message.answer()` and `callback.answer()` both reach the bot as a single
    awaited call carrying an aiogram method object (`SendMessage`,
    `AnswerCallbackQuery`), and both of those carry `.text` — the same shape
    `test_prelaunch_hold.py` asserts against.
    """
    bot = _mock_bot()
    await dp.feed_update(bot, update)
    if bot.await_count == 0:
        return None
    assert bot.await_count == 1, "exactly one side must answer, not both"
    return getattr(bot.await_args.args[0], "text", None)


# ---------------------------------------------------------------------------
# who gets in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_allowlisted_id_reaches_the_real_bot():
    dp = _dispatcher(frozenset({GRANT, CURATOR}))
    assert await _answered(dp, _message_update(GRANT)) == LIVE_ANSWER
    assert await _answered(dp, _message_update(CURATOR)) == LIVE_ANSWER


@pytest.mark.asyncio
async def test_everyone_else_still_meets_the_hold():
    """The failure this whole task risks: a member reaching a live checkout."""
    dp = _dispatcher(frozenset({GRANT}))
    assert await _answered(dp, _message_update(STRANGER)) == HOLD_ANSWER


@pytest.mark.asyncio
async def test_an_event_with_no_user_is_not_admitted():
    """Fail closed. Unknown must never mean allowed."""
    dp = _dispatcher(frozenset({GRANT}))
    assert await _answered(dp, _message_update(None)) == HOLD_ANSWER


@pytest.mark.asyncio
async def test_the_callback_observer_is_gated_too():
    """The bot's pre-10-August inline buttons are still tappable (GK-421).

    Gating messages and forgetting callbacks would leave every one of those
    buttons a live route into the flow it was posted for.
    """
    dp = _dispatcher(frozenset({GRANT}))
    assert await _answered(dp, _callback_update(GRANT)) == LIVE_ANSWER
    assert await _answered(dp, _callback_update(STRANGER)) == HOLD_ANSWER


@pytest.mark.asyncio
async def test_an_empty_allowlist_builds_no_gate_at_all():
    """The default deployment must be GK-443 unchanged, not GK-443 plus a filter."""
    dp = _dispatcher(frozenset())
    assert await _answered(dp, _message_update(GRANT)) == HOLD_ANSWER
    assert await _answered(dp, _message_update(STRANGER)) == HOLD_ANSWER


@pytest.mark.asyncio
async def test_the_practice_chat_stays_silent_for_an_allowlisted_id():
    """GK-438 outranks this. Grant on the allowlist does not make the bot
    speak in the room where the 10 August incident happened."""
    dp = _dispatcher(frozenset({GRANT}))
    assert await _answered(dp, _message_update(GRANT, chat_type="supergroup")) is None


# ---------------------------------------------------------------------------
# the gate's own shape
# ---------------------------------------------------------------------------


def test_both_observers_are_gated_by_the_one_same_predicate():
    """One decision taken once, not two copies free to drift apart.

    Reaches into `observer._handler.filters`, which is where aiogram 3 keeps
    router-level filters. Private, and worth it: this is what fails first and
    most legibly if a later edit drops or duplicates one of the two `.filter()`
    calls, and the behavioural tests above would then only say "a stranger got
    in" without saying why.
    """
    gate = handlers._prelaunch_allowlist_gate(frozenset({GRANT}), include=_stub_member_routers)
    message_filters = gate.message._handler.filters
    callback_filters = gate.callback_query._handler.filters
    assert len(message_filters) == 1
    assert len(callback_filters) == 1
    assert message_filters[0].callback is callback_filters[0].callback


def test_the_filter_reads_the_id_and_nothing_else():
    allowlisted = handlers._allowlist_filter(frozenset({GRANT}))
    assert allowlisted(SimpleNamespace(from_user=SimpleNamespace(id=GRANT))) is True
    assert allowlisted(SimpleNamespace(from_user=SimpleNamespace(id=STRANGER))) is False
    assert allowlisted(SimpleNamespace(from_user=None)) is False
    assert allowlisted(SimpleNamespace()) is False


# ---------------------------------------------------------------------------
# parsing `.env`, where the mistakes actually get made
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("   ", frozenset()),
        ("111111111", frozenset({111111111})),
        ("111111111,222222222", frozenset({111111111, 222222222})),
        (" 111111111 , 222222222 ", frozenset({111111111, 222222222})),
        ("111111111,,222222222,", frozenset({111111111, 222222222})),
        ("111111111,111111111", frozenset({111111111})),
    ],
)
def test_the_allowlist_parses_the_shapes_a_human_actually_types(raw, expected):
    assert parse_tg_id_allowlist(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "@GRANTPROGRESS",  # the mistake the docstring predicts
        "111111111,@GRANTPROGRESS",
        "grant",
        "111 111 111",
        "1e9",
    ],
)
def test_a_username_is_refused_rather_than_quietly_dropped(raw):
    """Dropping it would be fail-safe and invisible: the person meets the hold
    and reports a bug against the bot instead of against the `.env` line."""
    with pytest.raises(ValueError):
        parse_tg_id_allowlist(raw)


@pytest.mark.parametrize("raw", ["-1002368292795", "0", "111111111,-1001945266701"])
def test_a_chat_id_pasted_into_a_user_list_is_refused(raw):
    """`-1002368292795` is the practice chat. It would never match a user id, so
    the list would look populated and admit nobody."""
    with pytest.raises(ValueError):
        parse_tg_id_allowlist(raw)


def test_a_malformed_value_reads_as_empty_and_never_raises_at_router_build():
    """The property is read while the dispatcher is assembled. A bot that
    refuses to boot over a cosmetic typo during the launch window is worse than
    the failure it prevents — and here the degraded state is the safe one."""
    settings = Settings(_env_file=None, prelaunch_hold_allowlist="@GRANTPROGRESS")
    assert settings.prelaunch_hold_allowlist_ids == frozenset()


def test_a_malformed_value_still_refuses_the_boot_where_that_is_free():
    settings = Settings(_env_file=None, prelaunch_hold_allowlist="@GRANTPROGRESS")
    assert any("PRELAUNCH_HOLD_ALLOWLIST" in err for err in settings.validate_security())


def test_a_well_formed_value_is_not_a_security_error():
    settings = Settings(_env_file=None, prelaunch_hold_allowlist=f"{GRANT},{CURATOR}")
    assert not any("PRELAUNCH_HOLD_ALLOWLIST" in err for err in settings.validate_security())
    assert settings.prelaunch_hold_allowlist_ids == frozenset({GRANT, CURATOR})


def test_the_mechanism_is_off_until_somebody_turns_it_on():
    """Both halves default to the state that sells nothing to nobody."""
    settings = Settings(_env_file=None)
    assert settings.enable_prelaunch_hold is False
    assert settings.prelaunch_hold_allowlist == ""
    assert settings.prelaunch_hold_allowlist_ids == frozenset()


def test_the_key_is_covered_by_the_config_drift_guard():
    """GK-436's guard flags unknown keys that match one of our prefixes, so a
    misspelt `PRELAUNCH_HOLD_ALLOWLST` shows up as an orphan rather than as
    somebody else's variable. Without the prefix it would be invisible."""
    from app.config_audit import OURS_PREFIXES

    assert "PRELAUNCH_HOLD_ALLOWLIST".startswith(OURS_PREFIXES)
