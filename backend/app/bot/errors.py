"""Dispatcher-level error handling (GK-421).

Before this, `_build_dispatcher()` registered three middlewares and a router
and nothing else. Any exception a handler did not catch escaped aiogram
entirely: the callback query was never answered, so Telegram kept the button
spinning, and the member saw a product that simply did not respond. That is
what the 28.07 Stripe incident looked like from the buyer's side, and at launch
it is a lost sale that nobody hears about.

Per-handler `try/except` is still the right place to say something *specific*
("Lava did not create the invoice, try another method"). This layer exists so
that the case nobody anticipated is merely bad rather than invisible:

1. the member is told something happened and is not left waiting;
2. the spinner is stopped;
3. an ops alert goes out, rate-limited by exception type so a storm from one
   broken dependency does not become a thousand messages.

Deliberately never re-raises: an error handler that raises is worse than none.
"""
from __future__ import annotations

import logging

from aiogram import Dispatcher
from aiogram.types import CallbackQuery, ErrorEvent, Message, Update

from app.observability import send_ops_alert

logger = logging.getLogger(__name__)

#: Shown when we have nothing specific to say. It has to work for any handler,
#: so it promises nothing about what went wrong — only that the member is not
#: waiting for something that will never arrive, and that money is not involved.
GENERIC_ERROR_TEXT = (
    "Что-то пошло не так на нашей стороне. Ничего не списано.\n\n"
    "Попробуйте ещё раз через минуту или вернитесь в меню командой /start. "
    "Если повторится — напишите в поддержку, мы разберёмся."
)


def _extract_targets(update: Update | None) -> tuple[Message | None, CallbackQuery | None]:
    if update is None:
        return None, None
    callback = getattr(update, "callback_query", None)
    if callback is not None:
        return getattr(callback, "message", None), callback
    return getattr(update, "message", None), None


async def handle_bot_error(event: ErrorEvent) -> bool:
    """Answer the user, stop the spinner, alert ops. Never raises."""
    exception = event.exception
    update = getattr(event, "update", None)
    update_id = getattr(update, "update_id", None)

    logger.exception(
        "unhandled bot error update_id=%s exception=%s",
        update_id,
        exception.__class__.__name__,
        exc_info=exception,
    )

    message, callback = _extract_targets(update)

    # Stop the spinner first: it is the one thing the member can see, and if
    # sending a message fails we still do not want a hung button.
    if callback is not None:
        try:
            await callback.answer()
        except Exception:  # noqa: BLE001
            logger.warning("could not answer callback in error handler", exc_info=True)

    if message is not None:
        try:
            await message.answer(GENERIC_ERROR_TEXT)
        except Exception:  # noqa: BLE001
            logger.warning("could not deliver the error message to the user", exc_info=True)

    user = getattr(update, "event_from_user", None)
    try:
        await send_ops_alert(
            # GK-451: no markup. `send_ops_alert` escapes the whole body now, so
            # a <code> wrapper here would arrive as the literal characters
            # "&lt;code&gt;" — and this is the alert the task was filed on, the
            # one that died because an exception repr carried angle brackets.
            "Необработанная ошибка в боте\n"
            f"{exception.__class__.__name__}: {str(exception)[:300]}\n"
            f"update_id={update_id} user_id={getattr(user, 'id', None)}",
            # Rate-limit by exception type: one broken dependency otherwise
            # produces an alert per affected member.
            key=f"bot_unhandled:{exception.__class__.__name__}",
            rate_limit_seconds=600,
            severity="error",
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not send ops alert for unhandled bot error", exc_info=True)

    # True = handled. Returning False would let aiogram log it again and, in
    # webhook mode, answer the HTTP request with a 500 that Telegram retries.
    return True


def register_error_handler(dp: Dispatcher) -> None:
    dp.errors.register(handle_bot_error)
