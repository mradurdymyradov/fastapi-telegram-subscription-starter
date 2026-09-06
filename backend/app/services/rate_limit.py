"""Redis-backed rate limiter for sensitive endpoints (login, password reset, etc.).

Uses INCR+EXPIRE on a per-key bucket. Fail-open if Redis is unreachable —
better to let legitimate traffic through than DOS yourself; the Caddy layer
gives coarser DDoS protection.
"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Increment counter at `key`. Returns (allowed, current_count).

    allowed=False means caller should reject with 429.
    """
    try:
        r = _get_redis()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        count, _ = await pipe.execute()
        count = int(count)
        return count <= limit, count
    except Exception as e:
        # Fail-open: log but allow. Don't block real users on Redis flake.
        logger.warning("rate_limit hit failed for %s: %s", key, e)
        return True, 0


async def reset(key: str) -> None:
    try:
        await _get_redis().delete(key)
    except Exception as e:
        logger.warning("rate_limit reset failed for %s: %s", key, e)


async def is_token_revoked(jti: str) -> bool:
    if not jti:
        return False
    try:
        return bool(await _get_redis().exists(f"jwt:revoked:{jti}"))
    except Exception as e:
        # Fail-CLOSED for token revocation: if Redis is down, refuse the token.
        # An attacker should not benefit from a Redis outage.
        logger.error("revocation lookup failed for %s: %s — denying token", jti, e)
        return True


async def revoke_token(jti: str, ttl_seconds: int) -> None:
    if not jti:
        return
    try:
        await _get_redis().set(f"jwt:revoked:{jti}", "1", ex=max(ttl_seconds, 1))
    except Exception as e:
        logger.error("revoke_token failed for %s: %s", jti, e)
