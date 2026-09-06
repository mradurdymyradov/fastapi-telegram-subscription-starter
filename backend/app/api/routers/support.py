import html
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DB, CurrentAdmin
from app.db.models import SupportMessage, User
from app.services.audit import record as audit_record
from app.services.notifications import send_message

router = APIRouter(prefix="/support", tags=["support"])


class MessageOut(BaseModel):
    id: int
    user_id: int
    username: str | None
    first_name: str | None = None
    role: str
    content: str
    # GK-378: routing outcome for user tickets / delivery outcome for replies.
    delivery_status: str | None = None
    created_at: datetime


class ConversationOut(BaseModel):
    """One row per user who has ever written to support — the unit of the
    conversation-grouped admin inbox (GK-378), newest activity first."""

    user_id: int
    username: str | None
    first_name: str | None
    last_message: str
    last_role: str
    last_delivery_status: str | None
    last_message_at: datetime
    message_count: int
    # True when the most recent message is from the user (awaiting a reply).
    unanswered: bool


class ConversationsPage(BaseModel):
    items: list[ConversationOut]
    total: int


class ReplyIn(BaseModel):
    user_id: int = Field(..., ge=1)
    # 4096 is Telegram's hard per-message limit.
    content: str = Field(..., min_length=1, max_length=4096)


class ReplyOut(MessageOut):
    delivered: bool


@router.get("/conversations", response_model=ConversationsPage)
async def list_conversations(
    db: DB,
    _: CurrentAdmin,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    """Group the flat message log into one conversation per user, ordered by the
    most recent activity, so the admin inbox scales past a 100-message feed."""
    agg = (
        select(
            SupportMessage.user_id.label("user_id"),
            func.max(SupportMessage.id).label("last_id"),
            func.count().label("cnt"),
        )
        .group_by(SupportMessage.user_id)
        .subquery()
    )

    total = int(
        (await db.execute(select(func.count()).select_from(agg))).scalar_one() or 0
    )

    q = (
        select(SupportMessage, User, agg.c.cnt)
        .join(agg, agg.c.last_id == SupportMessage.id)
        .join(User, User.id == SupportMessage.user_id)
        .order_by(agg.c.last_id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(q)).all()
    items = [
        ConversationOut(
            user_id=u.id,
            username=u.username,
            first_name=u.first_name,
            last_message=m.content,
            last_role=m.role,
            last_delivery_status=m.delivery_status,
            last_message_at=m.created_at,
            message_count=int(cnt or 0),
            unanswered=m.role == "user",
        )
        for m, u, cnt in rows
    ]
    return ConversationsPage(items=items, total=total)


@router.get("/messages", response_model=list[MessageOut])
async def list_messages(
    db: DB,
    _: CurrentAdmin,
    user_id: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    """Messages newest-first. Pass `user_id` for a single user's transcript
    (used by both the conversation thread and the user-card history); the client
    reverses each page for chronological display and pages older via `offset`."""
    q = select(SupportMessage, User).join(User, User.id == SupportMessage.user_id)
    if user_id is not None:
        q = q.where(SupportMessage.user_id == user_id)
    q = q.order_by(SupportMessage.id.desc()).limit(limit).offset(offset)
    rows = (await db.execute(q)).all()
    return [
        MessageOut(
            id=m.id,
            user_id=u.id,
            username=u.username,
            first_name=u.first_name,
            role=m.role,
            content=m.content,
            delivery_status=m.delivery_status,
            created_at=m.created_at,
        )
        for m, u in rows
    ]


@router.post("/reply", response_model=ReplyOut)
async def reply_to_user(payload: ReplyIn, db: DB, admin: CurrentAdmin, request: Request):
    """Admin answers a user's support question straight from the panel.

    The reply is stored as an `assistant` message (so it shows in the thread and
    the panel) and delivered to the user through the bot. Like manual-payment
    approval, the API instantiates the bot on demand — bot and API don't share a
    process (CLAUDE.md). The delivery outcome is persisted on the row so the
    thread can show whether it actually reached the user.
    """
    user = (await db.execute(select(User).where(User.id == payload.user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    msg = SupportMessage(user_id=user.id, role="assistant", content=payload.content)
    db.add(msg)
    await db.flush()
    # Commit the transcript row before Telegram delivery. `flush()` alone is
    # rollback-able, which could otherwise deliver a reply that disappeared
    # from admin history if a later operation failed.
    await db.commit()

    # send_message renders HTML; escape so an admin's literal <, >, & can't break
    # Telegram parsing. The stored copy keeps the raw text for the panel.
    delivered = await send_message(user.tg_id, html.escape(payload.content))
    msg.delivery_status = "delivered" if delivered else "failed"

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="support.reply",
        target_type="user",
        target_id=user.id,
        details={"message_id": msg.id, "delivered": delivered, "len": len(payload.content)},
        request=request,
    )
    # Persist both the delivery outcome and its audit entry before responding.
    await db.commit()

    return ReplyOut(
        id=msg.id,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        role=msg.role,
        content=msg.content,
        delivery_status=msg.delivery_status,
        created_at=msg.created_at,
        delivered=delivered,
    )
