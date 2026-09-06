"""Read-only audit log view for administrators.

There is no UPDATE/DELETE endpoint by design — the table is append-only.
Records older than 1 year can be archived/pruned via a cron, but never by
the admin API.

The admin UI renders this as the moderation journal ("Журнал модерации"):
each row resolves the acting administrator's email and can be filtered by
action, by actor, or to the curated moderation-only action set.
"""
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, CurrentAdmin
from app.db.models import AdminUser, AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])

# Actions a curator/moderator performs on member-facing payments, refunds, and
# support requests. The journal filters to these by default so routine technical
# events (2FA, webhooks, plan/promo edits) don't bury moderation decisions.
# Keep in sync with the ACTION_LABELS map in admin/.../audit/page.tsx.
MODERATION_ACTIONS = (
    "payment.approve",
    "payment.reject",
    "payment.usdt_verify",
    "payment.refund.request",
    "payment.refund.resolve",
    "payment.refund.sync",
    "support.reply",
)


class AuditRow(BaseModel):
    id: int
    actor_admin_id: int | None
    actor_label: str | None
    actor_ip: str | None
    action: str
    target_type: str | None
    target_id: str | None
    details: dict
    created_at: datetime


class ActorRow(BaseModel):
    actor_admin_id: int
    email: str | None


@router.get("/log", response_model=list[AuditRow])
async def list_audit(
    db: DB,
    _: CurrentAdmin,
    action: str | None = Query(None, max_length=64),
    actor_admin_id: int | None = Query(None, ge=1),
    category: str | None = Query(None, max_length=32),
    limit: int = Query(100, ge=1, le=500),
):
    q = (
        select(AuditLog, AdminUser.email)
        .join(AdminUser, AdminUser.id == AuditLog.actor_admin_id, isouter=True)
        .order_by(AuditLog.id.desc())
    )
    if action:
        q = q.where(AuditLog.action == action)
    if actor_admin_id is not None:
        q = q.where(AuditLog.actor_admin_id == actor_admin_id)
    if category == "moderation":
        q = q.where(AuditLog.action.in_(MODERATION_ACTIONS))
    q = q.limit(limit)
    rows = (await db.execute(q)).all()
    return [
        AuditRow(
            id=r.id,
            actor_admin_id=r.actor_admin_id,
            actor_label=email,
            actor_ip=r.actor_ip,
            action=r.action,
            target_type=r.target_type,
            target_id=r.target_id,
            details=r.details or {},
            created_at=r.created_at,
        )
        for (r, email) in rows
    ]


@router.get("/actors", response_model=list[ActorRow])
async def list_actors(db: DB, _: CurrentAdmin):
    """Distinct administrators that appear in the log, for the actor filter."""
    q = (
        select(AuditLog.actor_admin_id, AdminUser.email)
        .join(AdminUser, AdminUser.id == AuditLog.actor_admin_id, isouter=True)
        .where(AuditLog.actor_admin_id.is_not(None))
        .group_by(AuditLog.actor_admin_id, AdminUser.email)
        .order_by(AdminUser.email)
    )
    rows = (await db.execute(q)).all()
    return [ActorRow(actor_admin_id=aid, email=email) for (aid, email) in rows]
