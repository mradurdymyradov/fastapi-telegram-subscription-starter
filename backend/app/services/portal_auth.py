"""Magic-link issuance + server-side session management for the member portal.

GK-091. Two token families, both 256-bit and stored only as SHA-256 hashes:

- **Magic link** — one-time, 15-min, issued by the bot after a `has_portal_access`
  check, redeemed by the portal `/auth/magic` route. Single-use via an atomic
  `used_at` stamp.
- **Session** — opaque cookie value backing a `PortalSession` row. No JWT, so a
  cancelled/kicked user is revoked instantly (a single UPDATE to `revoked_at`)
  and every protected request additionally re-runs `has_portal_access`.

The bot calls `issue_magic_link` directly on its own DB session (CLAUDE.md:
"Bot ⇄ API talk through Postgres + Redis, not direct HTTP"); the portal Next app
calls the redeem/load/revoke helpers through the FastAPI `/api/portal/*` routes.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import PortalMagicLink, PortalSession, User, utcnow
from app.services.subscription import has_portal_access

logger = logging.getLogger(__name__)
settings = get_settings()

_TOKEN_BYTES = 32  # 256 bits
_SESSION_SLIDE_THRESHOLD = timedelta(days=7)


@dataclass(frozen=True)
class RedeemResult:
    ok: bool
    session_token: str | None = None
    expires_at: datetime | None = None
    user_id: int | None = None
    # "invalid" (bad/expired/used token) or "no_access" (token fine, sub lapsed).
    reason: str | None = None


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_ua(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()


def magic_link_url(raw_token: str) -> str:
    base = settings.portal_base_url.rstrip("/")
    return f"{base}/auth/magic?token={raw_token}"


async def issue_magic_link(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
) -> str | None:
    """Issue a one-time portal login token for `user`, or None if no access.

    Caller (bot handler) is responsible for the access *messaging*; we still
    re-check `has_portal_access` here as the authoritative gate so a stale bot
    keyboard cannot mint a link for a lapsed subscriber.
    """
    if not await has_portal_access(session, user.id):
        return None

    now = now or utcnow()
    await _enforce_active_link_cap(session, user.id, now=now)

    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    link = PortalMagicLink(
        token_hash=_hash_token(raw),
        user_id=user.id,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.portal_magic_link_ttl_minutes),
    )
    session.add(link)
    await session.flush()
    return raw


async def _enforce_active_link_cap(
    session: AsyncSession, user_id: int, *, now: datetime
) -> None:
    """Invalidate oldest live links so a new issuance keeps <= max_active total."""
    cap = max(1, settings.portal_magic_link_max_active)
    rows = (
        await session.execute(
            select(PortalMagicLink)
            .where(
                PortalMagicLink.user_id == user_id,
                PortalMagicLink.used_at.is_(None),
                PortalMagicLink.expires_at > now,
            )
            .order_by(PortalMagicLink.created_at.asc())
        )
    ).scalars().all()
    # Keep at most (cap - 1) so the about-to-be-added link fits within `cap`.
    overflow = len(rows) - (cap - 1)
    for link in rows[: max(0, overflow)]:
        link.used_at = now


async def redeem_magic_link(
    session: AsyncSession,
    raw_token: str,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> RedeemResult:
    """Atomically consume a magic link and open a portal session."""
    now = now or utcnow()
    if not raw_token:
        return RedeemResult(ok=False, reason="invalid")

    link = (
        await session.execute(
            select(PortalMagicLink).where(
                PortalMagicLink.token_hash == _hash_token(raw_token)
            )
        )
    ).scalar_one_or_none()

    if link is None or link.used_at is not None or link.expires_at <= now:
        logger.info("portal magic-link redeem rejected: invalid/used/expired")
        return RedeemResult(ok=False, reason="invalid")

    # Atomic single-use claim: only mark used if still unused. A racing second
    # click finds rowcount 0 and is treated as already-redeemed.
    claimed = await session.execute(
        update(PortalMagicLink)
        .where(PortalMagicLink.id == link.id, PortalMagicLink.used_at.is_(None))
        .values(used_at=now)
    )
    if claimed.rowcount == 0:
        return RedeemResult(ok=False, reason="invalid")

    # Re-check access at redemption: a link minted 14 min ago by a now-cancelled
    # user must still be rejected.
    if not await has_portal_access(session, link.user_id):
        logger.info("portal magic-link redeem rejected: user %s lost access", link.user_id)
        return RedeemResult(ok=False, reason="no_access", user_id=link.user_id)

    raw_session, expires_at = await create_session(
        session, link.user_id, user_agent=user_agent, ip=ip, now=now
    )
    return RedeemResult(
        ok=True,
        session_token=raw_session,
        expires_at=expires_at,
        user_id=link.user_id,
    )


async def create_session(
    session: AsyncSession,
    user_id: int,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    now = now or utcnow()
    await _enforce_active_session_cap(session, user_id, now=now)

    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = now + timedelta(days=settings.portal_session_days)
    row = PortalSession(
        token_hash=_hash_token(raw),
        user_id=user_id,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
        user_agent_hash=_hash_ua(user_agent),
        ip_first_seen=ip,
        ip_last_seen=ip,
    )
    session.add(row)
    await session.flush()
    return raw, expires_at


async def _enforce_active_session_cap(
    session: AsyncSession, user_id: int, *, now: datetime
) -> None:
    """Make room for one new live session, serializing creation per account.

    Locking the stable ``users`` row gives every redemption for the same account
    one transaction-scoped mutex. The subsequent live-session query also locks
    the rows it may revoke, coordinating cleanly with logout/revoke operations.
    Once this helper returns, at most ``cap - 1`` live rows remain, so inserting
    the caller's new row cannot take the account over the configured cap.
    """
    cap = max(1, settings.portal_session_max_active)

    # Different magic links can be redeemed concurrently. Serialize their
    # session creation on the account rather than on the independent link rows.
    await session.execute(
        select(User.id).where(User.id == user_id).with_for_update()
    )

    rows = (
        await session.execute(
            select(PortalSession)
            .where(
                PortalSession.user_id == user_id,
                PortalSession.revoked_at.is_(None),
                PortalSession.expires_at > now,
            )
            .order_by(PortalSession.created_at.asc(), PortalSession.id.asc())
            .with_for_update()
        )
    ).scalars().all()

    # Keep at most (cap - 1) so the about-to-be-added session fits. Expired and
    # already-revoked rows are deliberately absent from ``rows`` and retained as
    # audit history without consuming a device slot.
    overflow = len(rows) - (cap - 1)
    for row in rows[: max(0, overflow)]:
        row.revoked_at = now


async def load_session(
    session: AsyncSession,
    raw_token: str | None,
    *,
    ip: str | None = None,
    now: datetime | None = None,
) -> PortalSession | None:
    """Return the live `PortalSession` for a cookie value, sliding its TTL.

    Returns None for a missing/revoked/expired session. Does NOT check
    subscription access — that is the dependency's job so `/me` can still load
    for a lapsed user while `/videos` is gated.
    """
    if not raw_token:
        return None
    now = now or utcnow()
    row = (
        await session.execute(
            select(PortalSession).where(PortalSession.token_hash == _hash_token(raw_token))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None

    row.last_seen_at = now
    if ip:
        row.ip_last_seen = ip
    # Sliding renewal: extend only when close to expiry to avoid a write storm.
    if row.expires_at - now < _SESSION_SLIDE_THRESHOLD:
        row.expires_at = now + timedelta(days=settings.portal_session_days)
    return row


async def revoke_session(
    session: AsyncSession, raw_token: str | None, *, now: datetime | None = None
) -> bool:
    if not raw_token:
        return False
    now = now or utcnow()
    result = await session.execute(
        update(PortalSession)
        .where(
            PortalSession.token_hash == _hash_token(raw_token),
            PortalSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return result.rowcount > 0


async def revoke_all_for_user(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> int:
    """Log a user out of every device (admin "revoke access" / hard cancel)."""
    now = now or utcnow()
    result = await session.execute(
        update(PortalSession)
        .where(PortalSession.user_id == user_id, PortalSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount or 0
