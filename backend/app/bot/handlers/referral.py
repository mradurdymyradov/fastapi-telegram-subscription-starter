import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.referral import leaderboard

router = Router(name="referral")


# GK-413: leaderboard copy is Grant's approved wording (button doc, image8) —
# an intro line, a static "Цели и награды" ladder, the live "Текущий топ", and a
# closing call to action. Only the ranking rows are dynamic; the example row in
# Grant's doc (@GRANTPROGRESS) is illustrative and must never be hardcoded.
#
# GK-452: Grant's 19.08 doc (Tab 3, «Заменить на следующую последовательность»)
# rewrote the last sentence — «награды, которых нет в подписке» replaces
# «награды, которые не купить за деньги». It ends without a full stop in his
# doc; that is his text, not a truncation, and it stays as written.
_LB_INTRO = (
    "Приглашайте людей на путь, зарабатывайте с каждой оплаты и поднимайтесь "
    "в топ. Лучших ждут награды, которых нет в подписке"
)

# GK-419 set the thresholds (50/75/100); GK-452 rotates the rewards up a rung
# per Grant's 19.08 doc (Tab 3). The 50 tier now gets Sacred Mushroom University
# «пакет Премиум», 75 gets the retreat at «скидкой 50 процентов», and the
# personal session with Owner moves to the 100 tier. Verbatim.
_LB_GOALS = (
    "<b>Цели и награды</b>\n"
    "🥉 50 приглашённых, обучение в Sacred Mushroom University, пакет Премиум\n"
    "🥈 75 приглашённых, участие в ретрите со скидкой 50 процентов\n"
    "🥇 100 приглашённых, личная сессия с Павлом, 60 минут один на один"
)

_LB_OUTRO = "Приглашайте друзей по вашей партнёрской ссылке и занимайте верхние строки."


def _plural_invited(n: int) -> str:
    """Russian agreement for «приглашённый»: an ones-digit of 1 (but not 11)
    takes the singular; every other count takes «приглашённых» (a substantivized
    adjective is genitive plural after 2–4 as well)."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "приглашённый"
    return "приглашённых"


def _format_board(rows) -> str:
    # B12 (GK-373): the program ranks by number of invited users, not legacy
    # bonus days.
    parts = ["🏆 <b>Лидерборд партнёров</b>", "", _LB_INTRO, "", _LB_GOALS, "", "<b>Текущий топ</b>"]
    if not rows:
        parts.append("Пока пусто — будьте первым!")
    else:
        medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 17
        for i, (user, count, _bonus) in enumerate(rows):
            # Escape: username/first_name come from Telegram and can contain HTML.
            raw = user.username and f"@{user.username}" or (user.first_name or "Аноним")
            name = html.escape(raw)
            parts.append(f"{medals[i]} {name}, {count} {_plural_invited(count)}")
    parts.append("")
    parts.append(_LB_OUTRO)
    return "\n".join(parts)


@router.message(Command("leaderboard"))
@router.message(F.text == "🏆 Лидерборд")
async def show_leaderboard(message: Message, session: AsyncSession):
    rows = await leaderboard(session, limit=20)
    await message.answer(_format_board(rows))


@router.callback_query(F.data == "leaderboard")
async def leaderboard_cb(cb: CallbackQuery, session: AsyncSession):
    rows = await leaderboard(session, limit=20)
    await cb.message.answer(_format_board(rows))
    await cb.answer()
