"""Operational alerts → Telegram chat `@alert_membership_community`.

Used for things humans must see now: bot down, scheduler stuck, backup
failure, restore needed. Not for user-facing notifications (those live in
`app.services.notifications`).

Design:
- Best-effort. Send failures log a warning and return False — never raise
  back into the caller (an alert path that breaks fulfillment would be
  worse than no alert).
- Cheap rate-limit via Redis SETEX so a screaming bug does not flood the
  alert chat. Falls back to no-rate-limit if Redis is unavailable.
- Uses aiogram if available (already a backend dep). API process imports
  this lazily; we don't want aiogram on the API hot import path.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Final

logger = logging.getLogger(__name__)

ALERT_RATE_LIMIT_PREFIX: Final[str] = "ops:alert:rl:"


def ops_alert_config_status(config: Any | None = None) -> dict[str, Any]:
    """Return non-secret ops-alert config status for health/logging."""
    if config is None:
        from app.config import get_settings

        config = get_settings()

    bot_token_configured = bool(getattr(config, "bot_token", ""))
    chat_id = str(getattr(config, "alert_chat_id", "") or "").strip()
    chat_configured = bool(chat_id)
    missing = []
    if not bot_token_configured:
        missing.append("BOT_TOKEN")
    if not chat_configured:
        missing.append("ALERT_CHAT_ID")
    return {
        "configured": not missing,
        "missing": missing,
        "bot_token_configured": bot_token_configured,
        "chat_configured": chat_configured,
        "chat_target_type": _chat_target_type(chat_id) if chat_configured else None,
    }


def _chat_target_type(chat_id: str) -> str:
    return "username" if chat_id.startswith("@") else "id"


def _telegram_chat_id(chat_id: str) -> int | str:
    value = chat_id.strip()
    if not value:
        raise ValueError("ALERT_CHAT_ID is empty")
    try:
        return int(value)
    except ValueError:
        return value


async def _check_rate_limit(key: str, window_seconds: int) -> bool:
    """Return True if we may send, False if rate-limited.

    Race-condition-tolerant: two concurrent senders both passing the check is
    acceptable — we'd rather alert twice than miss the first one.
    """
    try:
        from redis.asyncio import from_url  # type: ignore

        from app.config import get_settings

        settings = get_settings()
        r = from_url(settings.redis_url, decode_responses=True)
        try:
            existed = await r.set(
                ALERT_RATE_LIMIT_PREFIX + key,
                "1",
                nx=True,
                ex=window_seconds,
            )
            return existed is True
        finally:
            try:
                await r.aclose()  # type: ignore[attr-defined]
            except AttributeError:
                # redis<5: .close() + .wait_closed()
                await r.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("alert rate-limit check failed (%s); allowing alert", e)
        return True


async def send_ops_alert(
    text: str,
    *,
    key: str | None = None,
    rate_limit_seconds: int = 300,
    severity: str = "warn",
) -> bool:
    """Send an ops alert to the configured Telegram chat.

    Args:
        text: Plain-text message body. It is HTML-escaped before delivery.
        key: Stable rate-limit bucket, e.g. "bot_heartbeat_stale" or
            "backup_failed". Omit to disable rate-limiting for this call.
        rate_limit_seconds: How long to suppress identical keys.
        severity: "info" | "warn" | "error" — prefixed in the message.
    """
    from app.config import get_settings

    settings = get_settings()
    config_status = ops_alert_config_status(settings)
    if not config_status["configured"]:
        logger.warning(
            "ops alert dropped: missing config %s",
            ",".join(config_status["missing"]),
        )
        return False

    if key is not None:
        if not await _check_rate_limit(key, rate_limit_seconds):
            logger.debug("ops alert suppressed by rate-limit: %s", key)
            return False

    prefix = {"info": "ℹ️", "warn": "⚠️", "error": "🚨"}.get(severity, "⚠️")
    body = (
        f"{prefix} <b>membership_saas ops</b>\n"
        f"<code>{html.escape(settings.app_env)}</code>\n"
        f"{html.escape(text)}"
    )
    plain_body = f"{prefix} membership_saas ops\n{settings.app_env}\n{text}"

    try:
        from aiogram import Bot
        from aiogram.exceptions import TelegramAPIError
    except Exception as e:  # pragma: no cover
        logger.warning("ops alert: aiogram unavailable (%s)", e)
        return False

    try:
        bot = Bot(token=settings.bot_token)
    except Exception as e:  # noqa: BLE001 — a malformed token must not raise here
        # Constructing Bot validates the token shape. This used to sit outside
        # the try below, so a malformed ALERT bot token turned "we failed to
        # alert" into an exception in the caller — killing a scheduler job or
        # 500-ing a payment webhook. Alerting must never break the thing it
        # was meant to report on.
        logger.warning("ops alert: bot token rejected (%s)", e)
        return False

    try:
        await bot.send_message(
            chat_id=_telegram_chat_id(settings.alert_chat_id),
            text=body,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except TelegramAPIError as e:
        if "can't parse entities" in str(e).casefold():
            logger.warning("ops alert HTML rejected; retrying as plain text: %s", e)
            try:
                await bot.send_message(
                    chat_id=_telegram_chat_id(settings.alert_chat_id),
                    text=plain_body,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                return True
            except Exception as retry_error:  # noqa: BLE001
                logger.warning("ops alert plain-text retry failed: %s", retry_error)
                return False
        logger.warning("ops alert send failed: %s", e)
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("ops alert send failed before Telegram accepted request: %s", e)
        return False
    finally:
        await bot.session.close()
