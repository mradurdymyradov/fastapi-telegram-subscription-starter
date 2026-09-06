import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.services.referral import ensure_unique_code

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    """Ensures a `User` row exists for every incoming update; injects as data['user']."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if tg_user is None or session is None:
            return await handler(event, data)

        q = select(User).where(User.tg_id == tg_user.id)
        user = (await session.execute(q)).scalar_one_or_none()
        if user is None:
            code = await ensure_unique_code(session)
            await session.execute(
                insert(User)
                .values(
                    tg_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                    language=(tg_user.language_code or "ru")[:8],
                    referral_code=code,
                )
                .on_conflict_do_nothing(index_elements=[User.tg_id])
            )
            # A concurrent first update may have inserted the row while this
            # transaction was generating its referral code. PostgreSQL waits for
            # that transaction at ON CONFLICT, then this new SELECT sees its row.
            user = (await session.execute(q)).scalar_one_or_none()
            if user is None:  # pragma: no cover - database invariant guard
                raise RuntimeError(f"user upsert produced no row for tg_id={tg_user.id}")

        changed = False
        if user.username != tg_user.username:
            user.username = tg_user.username
            changed = True
        if user.first_name != tg_user.first_name:
            user.first_name = tg_user.first_name
            changed = True
        if changed:
            await session.flush()

        # GK-400: a ban must be enforced on every surface, including the bot.
        # `User.is_banned` is the durable, DB-backed authorization flag the portal
        # already honors; drop the update here so a banned member cannot keep using
        # the bot even if the Telegram membership kick failed or has not run yet.
        if getattr(user, "is_banned", False):
            logger.info("dropping update from banned user tg_id=%s", tg_user.id)
            return None

        data["user"] = user
        return await handler(event, data)
