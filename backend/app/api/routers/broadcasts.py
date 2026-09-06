from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentAdmin
from app.db.models import Broadcast, Subscription, User, utcnow
from app.db.session import async_session
from app.services.audit import record as audit_record
from app.services.notifications import broadcast as send_broadcast

router = APIRouter(prefix="/broadcasts", tags=["broadcasts"])


class BroadcastIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    # 4096 is Telegram's hard message length limit.
    message: str = Field(..., min_length=1, max_length=4096)
    segment: str = Field("all", pattern="^(all|active|expired)$")


class BroadcastOut(BaseModel):
    id: int
    title: str
    message: str
    segment: str
    status: str
    sent_count: int
    failed_count: int
    created_at: datetime
    sent_at: datetime | None


@router.get("", response_model=list[BroadcastOut])
async def list_broadcasts(db: DB, _: CurrentAdmin):
    rows = (await db.execute(select(Broadcast).order_by(Broadcast.id.desc()).limit(50))).scalars().all()
    return [
        BroadcastOut(
            id=b.id,
            title=b.title,
            message=b.message,
            segment=b.segment,
            status=b.status,
            sent_count=b.sent_count,
            failed_count=b.failed_count,
            created_at=b.created_at,
            sent_at=b.sent_at,
        )
        for b in rows
    ]


async def _resolve_segment(segment: str) -> list[int]:
    async with async_session() as s:
        if segment == "active":
            now = utcnow()
            rows = await s.execute(
                select(User.tg_id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.status == "active", Subscription.expires_at > now)
                .distinct()
            )
        elif segment == "expired":
            rows = await s.execute(
                select(User.tg_id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.status == "expired")
                .distinct()
            )
        else:
            rows = await s.execute(select(User.tg_id).where(User.is_banned.is_(False)))
        return [int(r[0]) for r in rows.all()]


async def _run_broadcast(broadcast_id: int) -> None:
    async with async_session() as s:
        b = (await s.execute(select(Broadcast).where(Broadcast.id == broadcast_id))).scalar_one_or_none()
        if not b:
            return
        b.status = "sending"
        await s.commit()
    tg_ids = await _resolve_segment(b.segment)
    sent, failed = await send_broadcast(tg_ids, b.message)
    async with async_session() as s:
        b2 = (await s.execute(select(Broadcast).where(Broadcast.id == broadcast_id))).scalar_one_or_none()
        if b2:
            b2.status = "sent"
            b2.sent_count = sent
            b2.failed_count = failed
            b2.sent_at = utcnow()
            await s.commit()


@router.post("", response_model=BroadcastOut)
async def create_broadcast(
    payload: BroadcastIn,
    bg: BackgroundTasks,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    b = Broadcast(title=payload.title, message=payload.message, segment=payload.segment, status="draft")
    db.add(b)
    await db.flush()
    # Broadcasts are loud and reversal isn't possible — always audit.
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="broadcast.create",
        target_type="broadcast",
        target_id=b.id,
        details={"segment": b.segment, "title": b.title, "message_len": len(b.message)},
        request=request,
    )
    # The background worker opens a separate session. Make the broadcast and
    # its audit row visible before scheduling the irreversible send; otherwise
    # it can run before get_db's post-response commit and silently miss the row.
    await db.commit()
    bg.add_task(_run_broadcast, b.id)
    return BroadcastOut(
        id=b.id,
        title=b.title,
        message=b.message,
        segment=b.segment,
        status=b.status,
        sent_count=0,
        failed_count=0,
        created_at=b.created_at,
        sent_at=None,
    )
