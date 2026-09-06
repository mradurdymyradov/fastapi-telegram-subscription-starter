import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import MENU_BUTTON_TEXTS, main_menu
from app.config import get_settings
from app.db.models import SupportMessage, User

logger = logging.getLogger(__name__)

router = Router(name="support")
settings = get_settings()

# Launch support is manual: the bot never auto-answers and is not framed as an
# AI assistant. A user's message is stored as a SupportMessage so admins can
# read it from the panel and follow up; the bot only confirms receipt.
# (AI auto-replies are intentionally not wired - see CLAUDE.md "AI support is
# out of launch scope".)
RECEIVED = "✅ Спасибо! Вопрос получен. Куратор ответит вам в Telegram, обычно в течение 24 часов."


class SupportFlow(StatesGroup):
    chatting = State()


@router.message(Command("support"))
@router.message(F.text == "💬 Поддержка")
async def support_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(SupportFlow.chatting)
    await message.answer(_prompt(), reply_markup=main_menu())


@router.callback_query(F.data == "support_start")
async def support_start_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SupportFlow.chatting)
    await cb.message.answer(_prompt(), reply_markup=main_menu())
    await cb.answer()


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Ок, диалог с поддержкой закрыт. Главное меню можно открыть через /start.",
        reply_markup=main_menu(),
    )


def _resolve_chat_id(raw: str) -> int | str:
    """A numeric chat_id (`-1001234567890`) must be an int for the Bot API;
    an `@username` target stays a string."""
    value = raw.strip()
    if value and (value.lstrip("-")).isdigit():
        return int(value)
    return value


async def route_support_message(bot: Bot, user: User, msg: SupportMessage) -> str:
    """Forward a persisted support ticket toward the curator chat and record the
    outcome on the message.

    GK-378: the message is already committed before this runs, so a
    routing failure never loses it — we only stamp `delivery_status` and log. When
    no curator chat is configured the ticket stays in the admin panel only.

    Returns the status string also written to `msg.delivery_status`:
    `routed` | `failed` | `skipped`.
    """
    target = (settings.support_routing_chat_id or "").strip()
    if not target:
        msg.delivery_status = "skipped"
        return "skipped"

    who = f"@{user.username}" if user.username else (user.first_name or f"User #{user.id}")
    text = (
        "💬 <b>Новый вопрос в поддержку</b>\n"
        f"От: {html.escape(who)} (id {user.id}, tg {user.tg_id})\n"
        "———\n"
        f"{html.escape(msg.content)}\n\n"
        "Ответьте пользователю из админ-панели → Поддержка."
    )
    try:
        await bot.send_message(chat_id=_resolve_chat_id(target), text=text)
        msg.delivery_status = "routed"
        return "routed"
    except TelegramAPIError as e:
        # Never re-raise: the user's ticket is already persisted and we still
        # confirm receipt. Curators can pick it up from the admin panel.
        logger.warning(
            "support routing to %s failed for support_message id=%s: %s",
            target,
            getattr(msg, "id", None),
            e,
        )
        msg.delivery_status = "failed"
        return "failed"


async def _capture(message: Message, session: AsyncSession, user: User) -> SupportMessage | None:
    """Store a user's message as a support ticket for admins. None on empty text."""
    text = (message.text or "").strip()
    if not text:
        return None
    msg = SupportMessage(user_id=user.id, role="user", content=text)
    session.add(msg)
    # A flush only sends the INSERT inside the current transaction; the bot
    # middleware could still roll it back if a later Telegram call failed.
    # Commit the ticket before any external side effect so routing can never
    # get ahead of the durable support history.
    await session.flush()
    await session.commit()
    return msg


# GK-453: `~F.text.in_(MENU_BUTTON_TEXTS)` is what keeps the reply keyboard
# alive inside the support dialog. Without it this handler claims every message
# in the state, so a member who taps «🎁 Подарить» while chatting to a curator
# files the words "🎁 Подарить" as a support ticket and gets the receipt — the
# bug Grant screenshotted on 19.08. Non-text messages (a photo, a voice note)
# resolve the filter to True and are still captured, exactly as before.
@router.message(SupportFlow.chatting, ~F.text.in_(MENU_BUTTON_TEXTS))
async def support_message(message: Message, session: AsyncSession, user: User):
    msg = await _capture(message, session, user)
    if msg is not None:
        await route_support_message(message.bot, user, msg)
        # Persist routed/failed/skipped before the acknowledgement send. The
        # ticket itself is already safe even if this status commit fails.
        await session.commit()
        await message.answer(RECEIVED, reply_markup=main_menu())


@router.message(StateFilter(None), F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTON_TEXTS))
async def support_fallback(message: Message, session: AsyncSession, user: User, state: FSMContext):
    """Plain text outside any FSM state becomes a support ticket for admins.

    GK-453: menu labels are not plain text. This handler runs with no state at
    all — the ordinary condition for a member sitting in the chat — so without
    the exclusion it swallows any keyboard tap whose own handler is registered
    after this router.
    """
    msg = await _capture(message, session, user)
    if msg is not None:
        await route_support_message(message.bot, user, msg)
        await session.commit()
        await state.set_state(SupportFlow.chatting)
        await message.answer(RECEIVED, reply_markup=main_menu())


def _prompt() -> str:
    # GK-413: support copy is Grant's approved wording (button doc, image7). The
    # optional direct-contact line is dynamic (shown only when configured) and is
    # not part of Grant's doc example, so it is preserved between the two blocks.
    lines = [
        "💬 <b>Поддержка</b>",
        "",
        "Опишите ваш вопрос одним сообщением. Куратор ответит вам в Telegram, обычно в течение 24 часов.",
    ]
    contact = (settings.support_contact or "").strip()
    if contact:
        lines.append(f"Если удобнее, можно написать напрямую: {contact}.")
    lines.append("")
    lines.append("Чтобы выйти из режима поддержки, нажмите /cancel.")
    return "\n".join(lines)
