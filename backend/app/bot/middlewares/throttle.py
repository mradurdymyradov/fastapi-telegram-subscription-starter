"""Per-user throttle for the bot.

Stops a single user from hammering the bot (and indirectly the OpenAI/Anthropic
provider that backs the support assistant). Uses Redis sliding window via INCR+EXPIRE
so the limit is enforced across bot/worker restarts and (potentially) multiple instances.

Fail-open on Redis errors — we'd rather serve real users than 500 them.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.types import User as TgUser

from app.services.rate_limit import hit as rl_hit

logger = logging.getLogger(__name__)


class ThrottleMiddleware(BaseMiddleware):
    """Allow at most `limit` updates per `window_seconds` per Telegram user."""

    def __init__(self, limit: int = 20, window_seconds: int = 10) -> None:
        self.limit = limit
        self.window = window_seconds

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        ok, count = await rl_hit(
            f"bot:throttle:{tg_user.id}", limit=self.limit, window_seconds=self.window
        )
        if not ok:
            # Tell the user once per burst, then silently drop the rest.
            if count == self.limit + 1 and isinstance(event, Message):
                try:
                    await event.answer(
                        "⏳ Слишком много сообщений подряд. Подождите несколько секунд."
                    )
                except Exception:
                    pass
            logger.info("throttled tg_user=%s count=%s", tg_user.id, count)
            return None
        return await handler(event, data)
