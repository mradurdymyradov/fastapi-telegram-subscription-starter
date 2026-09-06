"""Bot entry point into the Vimeo member portal (GK-091).

Issues a one-time magic link straight through `portal_auth.issue_magic_link` on
the bot's own DB session — no HTTP hop to the API (CLAUDE.md: bot ⇄ API talk
through Postgres + Redis). The link is sent as plain text (Telegram linkifies
it) rather than an inline URL button so it works for local `http://localhost`
dev as well as the production HTTPS domain.
"""
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.services.portal_auth import issue_magic_link, magic_link_url

router = Router(name="portal")

# GK-413: header follows Grant's approved wording (button doc, image4). The
# no-access variant isn't shown in the doc; only its header is aligned so the
# archive screen reads consistently whether or not access is granted.
_NO_ACCESS = (
    "🎬 <b>Архив Гипно-Коучинга</b>\n\n"
    "Закрытый видеоархив доступен только активным участникам. "
    "Оформите подписку через 💎 <b>Подписка</b> или /subscribe — и ссылка появится здесь."
)


async def send_archive_link(message: Message, session: AsyncSession, user: User) -> None:
    """Issue a magic link for `user` and DM it, or explain why access is denied.

    Shared by the reply-keyboard button and the `?start=portal` deep link.
    """
    raw = await issue_magic_link(session, user)
    if raw is None:
        await message.answer(_NO_ACCESS)
        return
    url = magic_link_url(raw)
    # GK-413: archive copy is Grant's approved wording (button doc, image4).
    await message.answer(
        "🎬 <b>Архив Гипно-Коучинга</b>\n\n"
        "Более 3000 уроков методологии, всё собрано в одном месте. "
        "Смотрите в удобном порядке, возвращайтесь к нужному в любой момент.\n\n"
        "Ваша персональная ссылка для входа в закрытый веб-архив:\n"
        f"{url}\n\n"
        "🔗 Ссылка действует <b>15 минут</b> и работает <b>один раз</b>. "
        "Не передавайте её другим. После входа браузер запомнит вас примерно на 30 дней.",
        disable_web_page_preview=True,
    )


@router.message(Command("archive"))
@router.message(F.text == "🎬 Открыть архив")
async def open_archive(message: Message, session: AsyncSession, user: User) -> None:
    await send_archive_link(message, session, user)


# `t.me/<bot>?start=portal` deep link from the portal landing page CTA. This
# router is registered BEFORE `start` so the "portal" payload is caught here;
# other payloads (ref_, paid_) don't match this filter and fall through to
# `start.py` untouched (which is write-locked by GK-021).
@router.message(CommandStart(magic=F.args == "portal"))
async def open_archive_deeplink(message: Message, session: AsyncSession, user: User) -> None:
    await send_archive_link(message, session, user)
