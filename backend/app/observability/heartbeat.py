"""Bot heartbeat — proves the bot process + APScheduler are alive.

The bot writes a UNIX timestamp to a Redis key on a tight interval (default
60s). The API `/health` endpoint reads the key and reports staleness so an
external uptime monitor (UptimeRobot, healthchecks.io, etc.) can page when
the bot/scheduler stalls even though the API container is still answering.

We use Redis (not Postgres) so heartbeat failures do not depend on the DB —
DB stalls are a separate signal the API surfaces independently.
"""

from __future__ import annotations

import logging
import time
from typing import Final

logger = logging.getLogger(__name__)

_HEARTBEAT_KEY: Final[str] = "ops:bot:heartbeat"
# Keep the key alive for ~5 minutes after the last beat. Anything older
# means the bot process is gone or the scheduler crashed.
HEARTBEAT_TTL_SECONDS: Final[int] = 300


def bot_heartbeat_key() -> str:
    return _HEARTBEAT_KEY


async def record_bot_heartbeat() -> None:
    """Best-effort: write `now` to Redis with the heartbeat key + TTL."""
    try:
        from redis.asyncio import from_url  # type: ignore

        from app.config import get_settings

        settings = get_settings()
        r = from_url(settings.redis_url, decode_responses=True)
        try:
            await r.set(_HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS)
        finally:
            try:
                await r.aclose()  # type: ignore[attr-defined]
            except AttributeError:
                await r.close()
    except Exception as e:  # noqa: BLE001
        # Heartbeat failure is logged at INFO — the next beat will retry,
        # and the API health endpoint will surface staleness anyway.
        logger.info("bot heartbeat write failed: %s", e)


async def seconds_since_last_heartbeat() -> int | None:
    """Return seconds since last heartbeat, or None if no key/Redis is down."""
    try:
        from redis.asyncio import from_url  # type: ignore

        from app.config import get_settings

        settings = get_settings()
        r = from_url(settings.redis_url, decode_responses=True)
        try:
            raw = await r.get(_HEARTBEAT_KEY)
            if raw is None:
                return None
            return max(0, int(time.time()) - int(raw))
        finally:
            try:
                await r.aclose()  # type: ignore[attr-defined]
            except AttributeError:
                await r.close()
    except Exception as e:  # noqa: BLE001
        logger.info("bot heartbeat read failed: %s", e)
        return None
