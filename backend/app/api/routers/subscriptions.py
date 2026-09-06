from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DB, CurrentAdmin
from app.db.models import Payment, Plan, Subscription, User, utcnow
from app.services.audit import record as audit_record
from app.services.subscription_cancellation import (
    buyer_email_from_payment,
    effective_cancel_state,
    open_manual_cancellation_filters,
)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


class SubOut(BaseModel):
    id: int
    user_id: int
    username: str | None
    plan_name: str
    status: str
    source: str
    started_at: datetime
    expires_at: datetime
    # GK-483: the client's own team. `status` still reads `active` because the
    # row behaves like any other for access; this is what tells the panel to
    # label it as team rather than as a subscriber, and it is why the row is
    # missing from `active_subs`. For a comp row `expires_at` is meaningless —
    # the access does not run out — which the panel says in as many words.
    is_comp: bool = False
    # GK-377: "cancel requested" and "provider actually stopped charging" are
    # different facts and the admin must be able to tell them apart.
    cancel_state: str | None = None
    cancel_requested_at: datetime | None = None
    cancel_confirmed_at: datetime | None = None
    cancel_resolved_at: datetime | None = None
    # GK-432: the bot stopped trying to remove this member from Telegram. Set
    # means they are still in the channel and only a human can change that —
    # the row is no longer pretending an attempt is in progress.
    access_revoke_abandoned_at: datetime | None = None
    access_revoke_error: str | None = None


class SubsPage(BaseModel):
    items: list[SubOut]
    total: int


class CancellationOut(BaseModel):
    """One row of the manual cancellation queue."""

    subscription_id: int
    user_id: int
    tg_id: int
    username: str | None
    plan_name: str
    provider: str | None
    #: NULL is normal for Lava RUB — the offer-page checkout produces no purchase
    #: webhook, so no contract id ever reaches us and only the Lava dashboard can
    #: stop the charge.
    provider_subscription_id: str | None
    buyer_email: str | None
    payment_id: int | None
    expires_at: datetime
    cancel_state: str | None
    cancel_requested_at: datetime | None
    cancel_failure_reason: str | None
    cancel_resolved_at: datetime | None


class CancellationsPage(BaseModel):
    items: list[CancellationOut]
    total: int


class ResolveCancellationIn(BaseModel):
    note: str | None = None


class SetCompIn(BaseModel):
    is_comp: bool
    #: Who this is — «куратор», «модератор», «Павел (второй аккаунт)». Not a
    #: column: it goes to the audit log, which is where "why does this member
    #: not count as paying" has to be answerable from months later.
    note: str | None = None


def _sub_out(s: Subscription, u: User, p: Plan) -> SubOut:
    return SubOut(
        id=s.id,
        user_id=u.id,
        username=u.username,
        plan_name=p.name,
        status=s.status,
        source=s.source,
        started_at=s.started_at,
        expires_at=s.expires_at,
        is_comp=bool(s.is_comp),
        cancel_state=effective_cancel_state(s),
        cancel_requested_at=s.cancel_requested_at,
        cancel_confirmed_at=s.cancel_confirmed_at,
        cancel_resolved_at=s.cancel_resolved_at,
        access_revoke_abandoned_at=s.access_revoke_abandoned_at,
        access_revoke_error=s.access_revoke_error if s.access_revoke_abandoned_at else None,
    )


@router.get("", response_model=SubsPage)
async def list_subs(
    db: DB,
    _: CurrentAdmin,
    status: str | None = Query(None, pattern="^(active|expired|cancelled|gifted)$"),
    # GK-483: `true` lists only the team, `false` only the real subscribers.
    # Unset keeps the old behaviour and lists both, so no existing caller moves.
    comp: bool | None = Query(None),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    base = select(Subscription, User, Plan).join(User, User.id == Subscription.user_id).join(Plan, Plan.id == Subscription.plan_id)
    if status:
        base = base.where(Subscription.status == status)
    if comp is not None:
        base = base.where(Subscription.is_comp.is_(comp))
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0)
    rows = (await db.execute(base.order_by(Subscription.id.desc()).limit(limit).offset(offset))).all()
    return SubsPage(items=[_sub_out(s, u, p) for s, u, p in rows], total=total)


@router.get("/cancellations", response_model=CancellationsPage)
async def list_cancellations(
    db: DB,
    _: CurrentAdmin,
    open_only: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    """Cancellation requests we could not confirm with the provider.

    This is the queue that has to be worked by hand — every row here is a member
    who asked to stop being charged and whose card is, as far as we know, still
    live at the provider.
    """
    base = (
        select(Subscription, User, Plan)
        .join(User, User.id == Subscription.user_id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.cancel_state.is_not(None))
    )
    if open_only:
        # GK-433: shared with the dashboard counter and the daily ops alert, so
        # the three can never report different numbers for the same queue.
        base = base.where(*open_manual_cancellation_filters())

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0)
    # Soonest charge first. Oldest-request-first was the wrong order for a queue
    # whose only deadline is the next renewal date.
    rows = (
        await db.execute(
            base.order_by(
                Subscription.expires_at.asc().nulls_last(),
                Subscription.cancel_requested_at.asc().nulls_last(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = []
    for s, u, p in rows:
        payment = await _latest_payment(db, s)
        items.append(
            CancellationOut(
                subscription_id=s.id,
                user_id=u.id,
                tg_id=u.tg_id,
                username=u.username,
                plan_name=p.name,
                provider=s.provider or s.source,
                provider_subscription_id=s.provider_subscription_id,
                buyer_email=buyer_email_from_payment(payment),
                payment_id=getattr(payment, "id", None),
                expires_at=s.expires_at,
                cancel_state=effective_cancel_state(s),
                cancel_requested_at=s.cancel_requested_at,
                cancel_failure_reason=s.cancel_failure_reason,
                cancel_resolved_at=s.cancel_resolved_at,
            )
        )
    return CancellationsPage(items=items, total=total)


@router.post("/{subscription_id}/cancellation/resolve")
async def resolve_cancellation(
    subscription_id: int,
    payload: ResolveCancellationIn,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    """Record that a human stopped the charge at the provider.

    Deliberately does NOT claim the provider confirmed anything — it records
    that an admin says they handled it. If the provider later sends its own
    cancellation webhook, ``effective_cancel_state`` upgrades the row to
    provider-confirmed on its own.
    """
    sub = (
        await db.execute(select(Subscription).where(Subscription.id == subscription_id))
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(404, "Subscription not found")
    if sub.cancel_state is None:
        raise HTTPException(400, "No cancellation request is on record for this subscription")
    if sub.cancel_resolved_at is not None:
        return {"ok": True, "already_resolved": True, "cancel_state": effective_cancel_state(sub)}

    sub.cancel_resolved_at = utcnow()
    sub.cancel_resolved_by_admin_id = admin.id
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="subscription.cancellation_resolved",
        target_type="subscription",
        target_id=sub.id,
        details={
            "provider": sub.provider or sub.source,
            "provider_subscription_id": sub.provider_subscription_id,
            "note": (payload.note or "")[:500],
        },
        request=request,
    )
    return {"ok": True, "cancel_state": effective_cancel_state(sub)}


@router.post("/{subscription_id}/comp")
async def set_comp(
    subscription_id: int,
    payload: SetCompIn,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
):
    """Mark a subscription as the client's own team, or take the mark off (GK-483).

    Grant, 2026-08-23: «Нас оставь и в чате, и в админ-панели, но отдельным
    статусом, не как платящих». This is the one place that mark is applied, and
    it is applied by a named human: who is on the team is a list only Grant can
    supply, and the task is explicit that it must not be inferred from who
    happens to be sitting in the chat. The audit row is the record of who said so.

    Setting it does two things at once, and they are the same decision: the row
    leaves `active_subs`, recognized revenue and churn, and its access stops
    running out — so the hourly `kick_expired_job` can no longer reach it.
    Clearing it puts both back, which is why an already-expired comp row becomes
    due for removal the moment the flag comes off. That is the intended shape:
    somebody leaves the team, their access ends like anyone else's.
    """
    sub = (
        await db.execute(select(Subscription).where(Subscription.id == subscription_id))
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(404, "Subscription not found")
    if bool(sub.is_comp) == payload.is_comp:
        return {"ok": True, "unchanged": True, "is_comp": bool(sub.is_comp)}

    sub.is_comp = payload.is_comp
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="subscription.comp_set" if payload.is_comp else "subscription.comp_cleared",
        target_type="subscription",
        target_id=sub.id,
        details={
            "user_id": sub.user_id,
            "status": sub.status,
            "source": sub.source,
            # Kept because it stops meaning anything while the flag is on, and
            # the value it had is what "access ends here again" reverts to.
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "note": (payload.note or "")[:500],
        },
        request=request,
    )
    return {"ok": True, "is_comp": bool(sub.is_comp)}


async def _latest_payment(db: DB, sub: Subscription) -> Payment | None:
    query = (
        select(Payment)
        .where(Payment.user_id == sub.user_id)
        .order_by(Payment.id.desc())
        .limit(1)
    )
    provider = sub.provider or sub.source
    if provider:
        query = query.where(Payment.provider == provider)
    return (await db.execute(query)).scalar_one_or_none()
