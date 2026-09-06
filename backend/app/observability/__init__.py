"""GK-040 ops baseline.

Single import surface for Sentry, structured JSON logging, correlation IDs,
Telegram alert sinks, and bot heartbeat. Both API (`app.api.main`) and bot
(`app.bot.main`) call `init_observability(service="api"|"bot")` at startup.

Nothing in this package may import aiogram at module top-level: the API
container must stay aiogram-free for fast cold-starts. Telegram alerts use
the low-level Bot client lazily, only when a DSN/chat is configured.
"""

from .alerts import ops_alert_config_status, send_ops_alert
from .heartbeat import (
    HEARTBEAT_TTL_SECONDS,
    bot_heartbeat_key,
    record_bot_heartbeat,
    seconds_since_last_heartbeat,
)
from .logging_setup import (
    bind_correlation_id,
    bind_log_context,
    clear_log_context,
    get_correlation_id,
    init_observability,
)

__all__ = [
    "init_observability",
    "bind_correlation_id",
    "bind_log_context",
    "clear_log_context",
    "get_correlation_id",
    "ops_alert_config_status",
    "send_ops_alert",
    "record_bot_heartbeat",
    "seconds_since_last_heartbeat",
    "bot_heartbeat_key",
    "HEARTBEAT_TTL_SECONDS",
]
