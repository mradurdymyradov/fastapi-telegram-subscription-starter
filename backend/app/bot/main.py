from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import timedelta
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.errors import register_error_handler
from app.bot.handlers import setup_handlers
from app.bot.middlewares import DbSessionMiddleware, ThrottleMiddleware, UserMiddleware
from app.bot.tasks import (
    backup_verification_job,
    heartbeat_job,
    kick_expired_job,
    manual_cancellation_queue_job,
    reconciliation_job,
    remind_expiring_job,
    scheduler_health_job,
    vimeo_sync_job,
)
from app.config import get_settings
from app.config_audit import enforce_configuration
from app.observability import init_observability

# JSON logging + Sentry. Replaces the stdlib basicConfig from the prototype.
init_observability(service="bot")

logger = logging.getLogger("bot")
settings = get_settings()

# GK-413 (image1) / GK-448: Grant's approved pre-Start bot description — the "What
# can this bot do?" text Telegram shows in an empty chat before the user presses
# Start. It lives in the Telegram bot profile, not in any message handler, so we
# apply it on startup via setMyDescription instead of leaving it to a manual
# BotFather step. Plain text only (descriptions do not render HTML); max 512 chars.
#
# GK-448, 2026-08-16 — Grant replaced the selling copy with this: «Описание бота
# (текст до "Старт") меняем на: "Официальный бот Сообщества Membership Club. Открытие
# скоро." Без даты и без цены специально, чтобы потом не править.» Verbatim, and it
# is deliberately shorter than what it replaced: the old text advertised a $19
# subscription to every member who opened an empty chat, which is the same pitch
# GK-443's hold exists to stop — and the hold cannot touch this field, because
# Telegram renders it before any update reaches us.
#
# The one thing it is NOT is permanent, whatever «чтобы потом не править» suggests:
# «Открытие скоро» expires on the day sales open (29.08), and on that day this
# field is the only thing a new member reads before pressing Start. Replacing it
# is on the launch runbook's step 5, not left to be noticed later.
#
# That was put to Grant and accepted the same day, so the replacement is decided
# rather than open: «За день до старта вернём продающий текст, тот, что у тебя
# сохранён. Я напомню накануне, поставишь его.» On 28.08 this constant goes back to
# the pre-GK-448 copy verbatim — `git show 48a447c^:backend/app/bot/main.py` — which
# is GK-413's approved text carrying GK-419's ⭐. It goes back HERE, in code: the
# function below rewrites the profile on every start when it differs, so editing the
# description by hand in BotFather is silently reverted by the next deploy.
#
# GK-419 history, still binding if selling copy ever returns here: 🍄 was replaced
# by ⭐ per Grant's «в крайнем случае ⭐». His preferred MycoTotems logo is a
# *custom emoji*, and setMyDescription takes a plain string with no
# parse_mode/entities parameter, so a custom emoji can never appear in this field.
# Do not put 🍄 back.
BOT_DESCRIPTION = "Официальный бот Сообщества Membership Club. Открытие скоро."


async def _apply_bot_profile(bot: Bot) -> None:
    """Apply Grant's approved bot description (GK-413, image1) to the Telegram
    profile. Best-effort and idempotent: we only write when it actually differs,
    and a transient API failure must never stop the bot from starting."""
    try:
        current = await bot.get_my_description()
        if (current.description or "") != BOT_DESCRIPTION:
            await bot.set_my_description(description=BOT_DESCRIPTION)
            logger.info("Applied bot description (GK-413 image1)")
    except Exception:  # noqa: BLE001 - profile copy is non-critical to bot operation
        logger.warning("Could not set bot description (non-fatal)", exc_info=True)


def _bot_update_mode(config: Any = settings) -> str:
    mode = (config.bot_update_mode or "polling").strip().lower()
    if mode not in {"polling", "webhook"}:
        raise RuntimeError("BOT_UPDATE_MODE must be polling or webhook")
    return mode


def _tg_webhook_path(config: Any = settings) -> str:
    path = (config.tg_webhook_path or "/tg-webhook/bot").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    if not path.startswith("/tg-webhook/"):
        raise RuntimeError("TG_WEBHOOK_PATH must start with /tg-webhook/")
    return path


def _tg_webhook_url(config: Any = settings) -> str:
    base_url = (config.tg_webhook_base_url or config.public_base_url).strip()
    if not base_url:
        raise RuntimeError("TG_WEBHOOK_BASE_URL or PUBLIC_BASE_URL is required for webhook mode")
    if config.is_prod and not base_url.startswith("https://"):
        raise RuntimeError("Telegram webhook URL must be https in production")
    return f"{base_url.rstrip('/')}{_tg_webhook_path(config)}"


def _tg_webhook_secret(config: Any = settings) -> str:
    secret = (config.tg_webhook_secret or "").strip()
    if not secret:
        raise RuntimeError("TG_WEBHOOK_SECRET is required for webhook mode")
    return secret


def _log_or_raise_config_errors() -> None:
    errors = settings.validate_security()
    if not errors:
        return
    message = "Security misconfiguration: " + " | ".join(errors)
    if settings.is_prod:
        raise RuntimeError(message)
    logger.warning("STARTUP WARNING: %s (env=%s)", message, settings.app_env)


def _build_storage() -> RedisStorage:
    # state_ttl/data_ttl (GK-350): abandoned FSM flows must self-expire —
    # a stuck BuyFlow state otherwise swallows the user's messages forever.
    return RedisStorage.from_url(
        settings.redis_url,
        state_ttl=timedelta(hours=24),
        data_ttl=timedelta(hours=24),
    )


def _build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=_build_storage())

    # Throttle FIRST: drop spam before we open a DB session or look up the user.
    dp.update.outer_middleware(ThrottleMiddleware(limit=20, window_seconds=10))
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(setup_handlers())
    # GK-421: last line of defence. Without it an unhandled exception leaves
    # the callback unanswered and the member staring at a spinning button.
    register_error_handler(dp)
    return dp


def _build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(kick_expired_job, "interval", hours=1, args=[bot], id="kick_expired")
    scheduler.add_job(remind_expiring_job, "cron", hour=10, minute=0, id="remind_expiring")
    # GK-070: daily provider/local discrepancy scan with ops summary link.
    scheduler.add_job(reconciliation_job, "cron", hour=5, minute=30, id="reconciliation")
    # GK-433: daily reminder while a cancellation still needs a human. Runs
    # after reconciliation, so a provider webhook that has already resolved an
    # item overnight has been reconciled before we nag anybody about it.
    scheduler.add_job(
        manual_cancellation_queue_job, "cron", hour=6, minute=0, id="manual_cancellation_queue"
    )
    # GK-091: refresh Vimeo archive metadata once a day at 04:00 UTC.
    scheduler.add_job(vimeo_sync_job, "cron", hour=4, minute=0, id="vimeo_sync")
    # GK-437: check that the restore canary is still proving something. Runs at
    # 06:30, after the canary's own 03:00 window, so a nightly verification has
    # had time to land before we judge its age.
    scheduler.add_job(
        backup_verification_job, "cron", hour=6, minute=30, id="backup_verification"
    )
    # GK-040: heartbeat every 60s - proves the bot loop + scheduler both run.
    # Health endpoint pages when no beat lands within BOT_HEARTBEAT_MAX_AGE.
    scheduler.add_job(heartbeat_job, "interval", seconds=60, id="bot_heartbeat")
    # Self-check every 15 min: if any other job missed its run, alert Telegram.
    scheduler.add_job(
        scheduler_health_job, "interval", minutes=15, args=[scheduler], id="scheduler_health"
    )
    return scheduler


async def _wait_for_shutdown_signal() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    registered_signals: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
            registered_signals.append(sig)
    try:
        await stop_event.wait()
    finally:
        for sig in registered_signals:
            with suppress(NotImplementedError):
                loop.remove_signal_handler(sig)


async def _run_polling(bot: Bot, dp: Dispatcher, bot_username: str) -> None:
    logger.info("Bot @%s starting in polling mode", bot_username)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def _run_webhook(bot: Bot, dp: Dispatcher, bot_username: str) -> None:
    path = _tg_webhook_path()
    webhook_url = _tg_webhook_url()
    secret = _tg_webhook_secret()

    app = web.Application()
    app.router.add_get("/tg-webhook/health", lambda _request: web.json_response({"status": "ok"}))
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=settings.tg_webhook_listen_host,
        port=settings.tg_webhook_listen_port,
    )

    try:
        await site.start()
        await bot.set_webhook(
            webhook_url,
            secret_token=secret,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        logger.info(
            "Bot @%s starting in webhook mode: url=%s listen=%s:%s path=%s",
            bot_username,
            webhook_url,
            settings.tg_webhook_listen_host,
            settings.tg_webhook_listen_port,
            path,
        )
        await _wait_for_shutdown_signal()
    finally:
        await runner.cleanup()


async def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is empty - set it in .env")
    _log_or_raise_config_errors()
    # GK-436: config drift is the reason GK-433 hid for sixteen days. Check the
    # environment against what the code actually reads, alert, and refuse to
    # start on an error — before a member finds it by pressing a button.
    await enforce_configuration("bot")

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = _build_dispatcher()
    scheduler = _build_scheduler(bot)
    scheduler.start()

    me = await bot.get_me()
    await _apply_bot_profile(bot)
    try:
        if _bot_update_mode() == "webhook":
            await _run_webhook(bot, dp, me.username)
        else:
            await _run_polling(bot, dp, me.username)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
