"""User-facing notifications sent FROM the bot (used by admin/cron flows).

We instantiate Bot here on demand instead of injecting it because admin API runs
in a different process — keep things simple, the volume is tiny.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def send_message(tg_id: int, text: str, reply_markup=None) -> bool:
    if not settings.bot_token:
        logger.warning("BOT_TOKEN not set, skipping message to %s", tg_id)
        return False
    bot = Bot(token=settings.bot_token)
    try:
        await bot.send_message(chat_id=tg_id, text=text, reply_markup=reply_markup, parse_mode="HTML")
        return True
    except TelegramAPIError as e:
        logger.warning("send_message(%s) failed: %s", tg_id, e)
        return False
    finally:
        await bot.session.close()


async def broadcast(tg_ids: list[int], text: str, throttle: float = 0.05) -> tuple[int, int]:
    sent = 0
    failed = 0
    if not settings.bot_token:
        return 0, len(tg_ids)
    bot = Bot(token=settings.bot_token)
    try:
        for tg_id in tg_ids:
            try:
                await bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
                sent += 1
            except TelegramAPIError:
                failed += 1
            await asyncio.sleep(throttle)  # ~20 msg/sec, well below TG limits
    finally:
        await bot.session.close()
    return sent, failed
