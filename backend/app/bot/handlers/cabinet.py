import html
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import cabinet_keyboard
from app.config import get_settings
from app.db.models import User
from app.services.referral_ledger import (
    InvitedFriend,
    PartnerOverview,
    partner_overview,
)
from app.services.subscription import get_active_subscription, is_comp_access

router = Router(name="cabinet")
settings = get_settings()

# BLK-005 money-partner program. Describes the *target* program rules Grant
# approved; the live ledger only records confirmed commissions today (recurring
# 12-month accrual is GK-020/GK-100 follow-up), so partner-facing numbers are
# always labelled as current ledger data and final amounts are confirmed by
# support — never presented as an already-calculated future total (GK-373).
_PARTNER_RULES = (
    # GK-413: rules wording is Grant's approved copy (button doc, image9). Kept
    # <b> emphasis on the key numbers; Grant's plain doc rendering is formatting,
    # not a de-emphasis instruction.
    "<b>Как работает партнёрская программа</b>\n"
    "• Вы получаете <b>20%</b> с каждой оплаты приглашённого, до 12 месяцев\n"
    "• Начисления подтверждаются после 3 месяцев непрерывной подписки друга\n"
    "• Выплаты ежемесячно через поддержку, от <b>$100</b> накопленных начислений\n\n"
    "Итоговую сумму к выплате подтверждает поддержка."
)


def _fmt_usd(amount: Decimal | float | int) -> str:
    return f"${Decimal(str(amount or 0)):.2f}"


def _ref_link(user: User) -> str:
    # referral_code is server-generated alphanumeric, but escape anyway for parity
    # with other HTML-escaped fields and to harden against future code changes.
    return f"https://t.me/{settings.bot_username}?start=ref_{html.escape(user.referral_code)}"


def _share_keyboard(raw_link: str) -> InlineKeyboardMarkup:
    """Invite keyboard: a Telegram deep-link share plus a raw copy-link button.

    The Telegram share button (``t.me/share/url``) only opens *inside* Telegram, so
    forwarding the invite anywhere else (WhatsApp, iMessage, email…) was impossible
    (issue #11). The second button uses Telegram's ``CopyTextButton`` to copy the
    *raw* referral link to the clipboard with one tap, ready to paste into any
    messenger.
    """
    share_text = "Заходи в закрытое сообщество — по моей ссылке скидка 20% на первый месяц:"
    # B04 (GK-373 / GK-372): urlencode defaults to quote_plus, which renders spaces
    # as "+" inside the prefilled Telegram message. Force %20 with quote_via=quote
    # so the recipient sees real spaces, not plus signs.
    share_url = (
        "https://t.me/share/url?"
        + urllib.parse.urlencode(
            {"url": raw_link, "text": share_text},
            quote_via=urllib.parse.quote,
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
            # The raw link is copied verbatim — no quote_plus, so it is never
            # mangled (no spaces in a referral link, but keep it byte-for-byte).
            [InlineKeyboardButton(text="🔗 Скопировать ссылку", copy_text=CopyTextButton(text=raw_link))],
        ]
    )


def _share_prompt_text(user: User) -> str:
    # GK-413: invite copy is Grant's approved wording (button doc, image5).
    return (
        "<b>🤝 Пригласить друга</b>\n\n"
        "Этот путь легче идти вместе. Отправьте другу вашу ссылку, он получит "
        "скидку 20% на первый месяц, а ваша партнёрская статистика будет "
        "в разделе 💰 Партнёрам.\n\n"
        f"<code>{_ref_link(user)}</code>"
    )


def _friend_line(friend: InvitedFriend) -> str:
    if friend.username:
        name = html.escape(f"@{friend.username}")
    elif friend.first_name:
        name = html.escape(friend.first_name)
    else:
        name = f"друг #{friend.user_id}"
    # GK-413: comma separator matches Grant's approved friend-line format (image9).
    if friend.has_paid:
        return f"• {name}, начислено {_fmt_usd(friend.accrued_usd)}"
    return f"• {name}, пока без оплаты"


def _partner_text(user: User, overview: PartnerOverview) -> str:
    raw_link = _ref_link(user)
    earnings = overview.earnings
    # GK-409 (issue #9): show money the moment a friend pays — accrued total
    # includes pending rows so the partner sees progress before the 3-month
    # vesting gate. "Накоплено всего" and "Доступно к выводу" are split so the
    # confirmed, withdrawable balance is never confused with the in-progress one.
    lines = [
        "<b>💰 Партнёрская программа</b>\n",
        "Ваша партнёрская ссылка:",
        f"<code>{raw_link}</code>\n",
        f"Приглашено: <b>{overview.invited_count}</b>",
        f"Оплатило: <b>{overview.paid_invited_count}</b>",
        # GK-413: parens dropped to match Grant's approved wording (button doc, image9).
        f"Накоплено всего по данным реестра: <b>{_fmt_usd(earnings.accrued_usd)}</b>",
        f"Доступно к выводу: <b>{_fmt_usd(earnings.available_usd)}</b>",
    ]
    if earnings.pending_usd > 0:
        lines.append(
            f"  • ожидает подтверждения (3 мес): {_fmt_usd(earnings.pending_usd)}"
        )
    if earnings.paid_usd > 0:
        lines.append(f"  • уже выплачено: {_fmt_usd(earnings.paid_usd)}")
    if overview.friends:
        lines.append("\n<b>Ваши приглашённые:</b>")
        lines.extend(_friend_line(friend) for friend in overview.friends)
        if overview.invited_count > len(overview.friends):
            lines.append(f"…и ещё {overview.invited_count - len(overview.friends)}")
    lines.append("")
    lines.append(_PARTNER_RULES)
    return "\n".join(lines)


async def _render(message_or_cb, session: AsyncSession, user: User):
    sub = await get_active_subscription(session, user.id)
    overview = await partner_overview(session, user.id, friends_limit=5)
    accrued = overview.earnings.accrued_usd
    available = overview.earnings.available_usd
    partner_lines = (
        f"Приглашено друзей: <b>{overview.invited_count}</b> · оплатило: <b>{overview.paid_invited_count}</b>\n"
        f"Накоплено всего (по данным реестра): <b>{_fmt_usd(accrued)}</b>\n"
        f"Доступно к выводу: <b>{_fmt_usd(available)}</b>"
    )
    if sub:
        # GK-483: a team row has no expiry to count down to. This line used to
        # read «Истекает: 10.07.2026 (осталось ~0 дн.)» to a member whose access is
        # not going anywhere — a countdown to a date nothing acts on any more.
        if is_comp_access(sub):
            access_line = "Доступ: <b>бессрочный</b> (команда)"
        else:
            remaining = (sub.expires_at - datetime.now(UTC)).days
            access_line = (
                f"Истекает: <b>{sub.expires_at.strftime('%d.%m.%Y')}</b> "
                f"(осталось ~{max(remaining, 0)} дн.)"
            )
        text = (
            f"<b>👤 Личный кабинет</b>\n\n"
            f"Подписка: <b>активна</b>\n"
            f"{access_line}\n"
            f"{partner_lines}\n\n"
            f"Ваша партнёрская ссылка:\n<code>{_ref_link(user)}</code>"
        )
        markup = cabinet_keyboard(has_active_sub=True)
    else:
        text = (
            f"<b>👤 Личный кабинет</b>\n\n"
            f"Подписка: <i>не активна</i>\n"
            f"{partner_lines}\n\n"
            f"Ваша партнёрская ссылка (работает уже сейчас):\n<code>{_ref_link(user)}</code>"
        )
        markup = cabinet_keyboard(has_active_sub=False)

    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.answer(text, reply_markup=markup)
        await message_or_cb.answer()
    else:
        await message_or_cb.answer(text, reply_markup=markup)


@router.message(Command("cabinet"))
@router.message(F.text == "👤 Кабинет")
async def cabinet_cmd(message: Message, session: AsyncSession, user: User):
    await _render(message, session, user)


async def _send_partner_view(message: Message, session: AsyncSession, user: User) -> None:
    overview = await partner_overview(session, user.id, friends_limit=10)
    await message.answer(
        _partner_text(user, overview),
        reply_markup=_share_keyboard(_ref_link(user)),
    )


@router.message(Command("invite"))
@router.message(F.text == "🤝 Пригласить друга")
async def invite_friend_cmd(message: Message, user: User):
    raw_link = _ref_link(user)
    await message.answer(_share_prompt_text(user), reply_markup=_share_keyboard(raw_link))


@router.message(Command("partner"))
@router.message(F.text == "💰 Партнёрам")
async def partner_cmd(message: Message, session: AsyncSession, user: User):
    await _send_partner_view(message, session, user)


@router.callback_query(F.data == "my_ref_link")
async def my_ref_link(cb: CallbackQuery, session: AsyncSession, user: User):
    await _send_partner_view(cb.message, session, user)
    await cb.answer()
