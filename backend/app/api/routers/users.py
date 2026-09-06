import logging
from datetime import datetime

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select

from app.api.deps import DB, CurrentAdmin
from app.config import get_settings
from app.db.models import Subscription, User, utcnow
from app.services.audit import record as audit_record
from app.services.channel_access import (
    SubscriptionAccessRevokeResult,
    create_invite_links,
    revoke_subscription_access,
)
from app.services.subscription import (
    ACCESS_HOLDING_STATUSES,
    is_comp_access,
    record_access_revoke_attempt,
    subscription_access_ends_at,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/users", tags=["users"])


class UserOut(BaseModel):
    id: int
    tg_id: int
    username: str | None
    first_name: str | None
    joined_at: datetime
    referral_code: str
    bonus_days: int
    is_banned: bool
    subscription_status: str
    subscription_expires_at: datetime | None


class UsersPage(BaseModel):
    items: list[UserOut]
    total: int


class UserActionIn(BaseModel):
    action: str = Field(..., pattern="^(ban|unban|extend_days)$")
    days: int | None = Field(default=None, ge=1, le=3650)


@router.get("", response_model=UsersPage)
async def list_users(
    db: DB,
    _: CurrentAdmin,
    q: str = Query("", max_length=128, description="username or first_name or tg_id"),
    sub_status: str | None = Query(None, pattern="^(active|expired|none)$"),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    base = select(User)
    if q:
        # Escape LIKE wildcards so users can't trigger pathological scans.
        safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{safe}%"
        conditions = [
            User.username.ilike(like, escape="\\"),
            User.first_name.ilike(like, escape="\\"),
        ]
        if q.isdigit():
            conditions.append(cast(User.tg_id, String).ilike(like, escape="\\"))
        base = base.where(or_(*conditions))

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0)
    rows = (await db.execute(base.order_by(User.id.desc()).limit(limit).offset(offset))).scalars().all()

    items: list[UserOut] = []
    now = utcnow()
    for u in rows:
        sub = (
            await db.execute(
                select(Subscription)
                .where(Subscription.user_id == u.id)
                .order_by(Subscription.expires_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if sub and sub.status == "active" and sub.expires_at > now:
            status = "active"
            expires = sub.expires_at
        elif sub:
            status = "expired"
            expires = sub.expires_at
        else:
            status = "none"
            expires = None
        if sub_status and sub_status != status:
            continue
        items.append(
            UserOut(
                id=u.id,
                tg_id=u.tg_id,
                username=u.username,
                first_name=u.first_name,
                joined_at=u.joined_at,
                referral_code=u.referral_code,
                bonus_days=u.bonus_days,
                is_banned=u.is_banned,
                subscription_status=status,
                subscription_expires_at=expires,
            )
        )
    return UsersPage(items=items, total=total)


@router.post("/{user_id}/actions")
async def user_action(
    user_id: int, payload: UserActionIn, db: DB, admin: CurrentAdmin, request: Request
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "User not found")

    audit_details: dict = {"action": payload.action}
    action_result: dict = {}

    if payload.action == "ban":
        # GK-400: a ban must revoke access on every surface, not just flip the flag.
        # The bot middleware denies banned users immediately; here we also kick them
        # from the Telegram resources and revoke stored invites now (best effort),
        # recording each attempt so the hourly kick job retries any failure.
        user.is_banned = True
        revoked, failed, revoke_error = await _revoke_banned_user_access(db, user)
        audit_details["access_revoked"] = revoked
        if failed:
            audit_details["access_revoke_failed"] = failed
        if revoke_error:
            audit_details["access_revoke_error"] = revoke_error
        action_result = {"access_revoked": revoked, "access_revoke_failed": failed}
    elif payload.action == "unban":
        # GK-400: unban never silently restores the old (kicked) membership. For a
        # subscription still inside its paid window we clear the ban-revoke and issue
        # a fresh one-time invite; subscriptions that expired while banned stay revoked.
        user.is_banned = False
        restored = await _restore_unbanned_user_access(db, user)
        audit_details["access_restored"] = restored
        action_result = {"access_restored": restored}
    elif payload.action == "extend_days":
        if not payload.days:
            raise HTTPException(400, "days is required")
        sub = (
            await db.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.expires_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if sub and sub.status == "active":
            from datetime import timedelta
            sub.expires_at = sub.expires_at + timedelta(days=payload.days)
            audit_details["target"] = "subscription"
        else:
            user.bonus_days = (user.bonus_days or 0) + payload.days
            audit_details["target"] = "bonus_days"
        audit_details["days"] = payload.days
    else:
        # pydantic already validated, but keep as defense in depth
        raise HTTPException(400, "Unknown action")

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action=f"user.{payload.action}",
        target_type="user",
        target_id=user.id,
        details=audit_details,
        request=request,
    )
    return {"ok": True, **action_result}


async def _revoke_banned_user_access(db, user: User) -> tuple[int, int, str | None]:
    """Immediately revoke Telegram access for every access-holding subscription of a
    just-banned user. Best effort: each attempt is recorded (without expiring the
    paid subscription, so unban can restore it), and any failure leaves
    ``access_revoked_at`` NULL so the hourly kick job retries. Returns
    ``(revoked, failed, error_summary)``.
    """
    if not settings.bot_token:
        return (0, 0, None)
    subs = (
        (
            await db.execute(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.status.in_(ACCESS_HOLDING_STATUSES),
                    Subscription.access_revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not subs:
        return (0, 0, None)

    revoked = 0
    failed = 0
    errors: list[str] = []
    bot = Bot(token=settings.bot_token)
    try:
        for sub in subs:
            try:
                result = await revoke_subscription_access(
                    bot, user.tg_id, getattr(sub, "invite_link", None)
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.exception("ban revoke crashed for subscription %s", sub.id)
                result = SubscriptionAccessRevokeResult(False, error=str(e))
            record_access_revoke_attempt(
                sub,
                success=result.success,
                retry_after_seconds=result.retry_after,
                error=result.error,
                mark_status_expired=False,
            )
            if result.success:
                revoked += 1
            else:
                failed += 1
                if result.error:
                    errors.append(result.error)
    finally:
        await bot.session.close()
    return (revoked, failed, "; ".join(errors) or None)


async def _restore_unbanned_user_access(db, user: User) -> int:
    """Entitlement-aware restore on unban. For each ban-revoked subscription still
    inside its paid window, clear the revoke (restores portal access) and issue a
    FRESH one-time invite — never the old, already-distributed link. Subscriptions
    that expired while banned stay revoked. Returns the count restored.
    """
    subs = (
        (
            await db.execute(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.status.in_(ACCESS_HOLDING_STATUSES),
                    Subscription.access_revoked_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    entitled = [
        sub
        for sub in subs
        # GK-483: a comp row has no end date to be inside of, so the date test
        # would refuse to restore a team member an admin had banned and unbanned.
        if is_comp_access(sub)
        or ((ends_at := subscription_access_ends_at(sub)) is not None and ends_at > now)
    ]
    if not entitled:
        return 0

    restored = 0
    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    try:
        for sub in entitled:
            sub.access_revoked_at = None
            sub.access_revoke_retry_after_at = None
            sub.access_revoke_error = None
            if bot is not None:
                try:
                    result = await create_invite_links(bot, name=f"unban {user.id}")
                except Exception:  # pragma: no cover - defensive
                    logger.exception("unban invite re-issue crashed for subscription %s", sub.id)
                    result = None
                if result is not None and result.any_success:
                    sub.invite_link = result.storage_text
                    try:
                        await bot.send_message(
                            user.tg_id,
                            "Доступ восстановлен. Новая ссылка для входа:\n"
                            f"{result.storage_text}",
                        )
                    except Exception:  # pragma: no cover - best effort
                        pass
            restored += 1
    finally:
        if bot is not None:
            await bot.session.close()
    return restored
