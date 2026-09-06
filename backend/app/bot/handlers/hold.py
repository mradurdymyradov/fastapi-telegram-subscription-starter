"""GK-443: the pre-launch hold — the bot comes back silent, not selling.

GK-438 stopped the bot answering in groups. It does nothing about private
chats, where the bot still offers a paid subscription that cannot be bought
yet, to members of a community that stays free until 15.09. Roughly forty people
reached it privately in the four hours before it was stopped on 10 August. So
GK-438 makes the bot safe to *run*; this makes it safe to *meet*.

The hold comes off when sales open on 29.08 (Grant moved both dates on 16.08;
they were ~19.08 and 01.09 when this was written).

While `ENABLE_PRELAUNCH_HOLD` is on, `setup_handlers()` registers this router
and **returns without registering any other**. That is deliberate and it is the
whole design: the guarantee is not "the hold router is checked first", it is
that the handlers which create checkouts, issue portal links and open support
flows are *not in the dispatcher at all*. No future edit can slip a handler in
ahead of it, and "no checkout is created by any path" is true because the code
that creates one is unreachable rather than because a filter is expected to
catch it.

Support is not a branch here. It rides inside the one message, as the
`@GKcurators` handle Grant put there — a member who needs a human taps it and
talks to a human, without the bot brokering anything.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

router = Router(name="prelaunch_hold")

#: The one member-facing string this mode can produce. Every private message,
#: command and planted-keyboard tap resolves to exactly this text.
#:
#: **APPROVED unchanged by Grant on 2026-08-16** — «Текст заглушки утверждаю,
#: ставь как есть.» It went to him on 13.08 as a draft (his 12.08 instruction:
#: «Заглушку ставим… Пришли мне текст на утверждение до включения, без слова про
#: "оформите подписку"») and came back with no edits, so what is below is now
#: client-approved copy: change it only on his say-so, character for character.
#:
#: He also refused an addition in the same message: **do not add a paragraph
#: about 10 August.** «Прошло тихо, официальных объявлений не было, реагировать и
#: напоминать не нужно.» An apology or explanation here would tell ~4.5k people
#: about an incident most of them never noticed.
#:
#: One line ages with the calendar rather than with the text: «канал и чат
#: работают как раньше, доступ никуда не пропадает» is true until 15.09, when
#: non-payers are removed (GK-016). The hold comes off on 29.08, well before
#: that, so it never becomes a false promise — but if the hold is ever extended
#: past 15.09, this sentence is the thing that stops being true.
HOLD_MESSAGE = (
    "⏳ Бот сейчас в настройке.\n\n"
    "Мы готовим его к открытию клуба. Пока ничего не меняется: канал и чат "
    "«Практика и проработки» работают как раньше, доступ никуда не пропадает.\n\n"
    "Мы напишем здесь, когда бот будет готов.\n\n"
    "Если нужна помощь — @GKcurators"
)


@router.message()
async def hold_message(message: Message, state: FSMContext) -> None:
    """Any private message, command or keyboard tap: one answer, no keyboard.

    The FSM state is cleared rather than preserved. A member caught mid-flow
    when the hold went on cannot finish that flow anyway, and a state left
    sitting in Redis would swallow their first free-text message after the hold
    lifts — the GK-350 failure, arriving weeks later with no obvious cause.

    No `reply_markup`: the menu members already have planted on their clients
    stays as Grant decided (GK-438), but the hold never plants another one.
    """
    await state.clear()
    await message.answer(HOLD_MESSAGE, disable_web_page_preview=True)


@router.callback_query()
async def hold_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Inline buttons the bot posted before 10 August are still tappable.

    Answer the callback first, always: an unanswered callback leaves the button
    spinning, which is the GK-421 failure and reads to a member as a dead
    product. The text then goes to the chat the button sits in, and only when
    that chat is private — a callback whose message Telegram no longer has
    (`None`, or an `InaccessibleMessage`) carries no safe destination, so it
    gets the spinner stopped and nothing else rather than a guessed DM that
    could fail or, worse, land somewhere public.
    """
    await state.clear()
    await callback.answer()
    chat = getattr(callback.message, "chat", None)
    if chat is None or chat.type != ChatType.PRIVATE:
        logger.info(
            "prelaunch hold: spinner stopped for a callback with no private chat to answer in"
        )
        return
    await callback.bot.send_message(chat.id, HOLD_MESSAGE, disable_web_page_preview=True)
