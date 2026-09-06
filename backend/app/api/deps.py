from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminUser, User
from app.db.session import async_session
from app.services.portal_auth import load_session
from app.services.rate_limit import is_token_revoked
from app.services.security import decode_token
from app.services.subscription import has_portal_access


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Generic 401 to avoid telling attackers which step failed.
_AUTH_ERR = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


async def current_admin(
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    authorization: str | None = Header(default=None),
) -> AdminUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _AUTH_ERR
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if not payload or "sub" not in payload:
        raise _AUTH_ERR
    jti = payload.get("jti")
    if not jti or await is_token_revoked(jti):
        raise _AUTH_ERR
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        raise _AUTH_ERR from None
    admin = (await db.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()
    if admin is None or not admin.is_active:
        raise _AUTH_ERR
    # GK-405: account-epoch check. The token carries the token_version it was minted
    # with (`ver`); a credential change bumps admin.token_version so every older token
    # fails here even before its 4h expiry. Legacy tokens (no `ver`) count as epoch 0.
    if int(payload.get("ver") or 0) != admin.token_version:
        raise _AUTH_ERR
    # Stash jti+exp on the request so /auth/logout can revoke without re-parsing.
    request.state.jwt_jti = jti
    request.state.jwt_exp = int(payload.get("exp") or 0)
    return admin


CurrentAdmin = Annotated[AdminUser, Depends(current_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ─── Member portal cookie session (GK-091) ──────────────────────────────
PORTAL_COOKIE = "membership_portal_session"
_PORTAL_AUTH_ERR = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
_PORTAL_ACCESS_ERR = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="Subscription inactive"
)


def _portal_client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


async def current_portal_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    membership_portal_session: Annotated[str | None, Cookie()] = None,
) -> User:
    """Resolve the user behind a valid portal session cookie (401 otherwise).

    Does NOT require active access, so `/me` and `/logout` still work for a
    lapsed subscriber. Subscription gating is `current_portal_member`.
    """
    sess = await load_session(db, membership_portal_session, ip=_portal_client_ip(request))
    if sess is None:
        raise _PORTAL_AUTH_ERR
    user = (await db.execute(select(User).where(User.id == sess.user_id))).scalar_one_or_none()
    if user is None or user.is_banned:
        raise _PORTAL_AUTH_ERR
    request.state.portal_user_id = user.id
    return user


CurrentPortalUser = Annotated[User, Depends(current_portal_user)]


async def current_portal_member(
    user: CurrentPortalUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Portal user WITH active subscription access (403 when access has lapsed)."""
    if not await has_portal_access(db, user.id):
        raise _PORTAL_ACCESS_ERR
    return user


CurrentPortalMember = Annotated[User, Depends(current_portal_member)]
