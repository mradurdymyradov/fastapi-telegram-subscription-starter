from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased

from app.api.deps import DB, CurrentAdmin
from app.db.models import ReferralAttribution, ReferralCommission, ReferralPayoutBatch, User, utcnow
from app.services.audit import record as audit_record
from app.services.referral import leaderboard
from app.services.referral_ledger import (
    PAYOUT_CANCELLED,
    PAYOUT_DRAFT,
    PAYOUT_PAID,
    PAYOUT_SENT,
    PAYOUT_THRESHOLD_USD,
    PayoutTransitionError,
    create_referral_payout_batch,
    transition_referral_payout_batch,
    vest_due_commissions,
)

router = APIRouter(prefix="/referrals", tags=["referrals"])

# Cap on total nodes visited in the referral tree to prevent runaway recursion
# on very wide trees even when depth is bounded.
_MAX_TREE_NODES = 500


class LeaderRow(BaseModel):
    user_id: int
    username: str | None
    first_name: str | None
    referrals: int
    bonus_days: int


class TreeNode(BaseModel):
    user_id: int
    username: str | None
    joined_at: datetime
    children: list["TreeNode"] = []


class SuspiciousAttributionRow(BaseModel):
    attribution_id: int
    referrer_id: int
    referrer_username: str | None
    referee_id: int
    referee_username: str | None
    source: str
    code: str | None
    attributed_at: datetime
    review_status: str
    suspicious_reason: str | None
    ignored_attempt_count: int
    last_ignored_referrer_id: int | None
    last_ignored_referrer_username: str | None
    last_ignored_source: str | None
    last_ignored_code: str | None
    last_ignored_at: datetime | None


class PayoutCommissionOut(BaseModel):
    id: int
    referrer_id: int
    referrer_username: str | None
    referrer_first_name: str | None
    referee_id: int
    referee_username: str | None
    referee_first_name: str | None
    source_provider: str
    source_invoice_id: str | None
    source_amount: float
    source_currency: str
    #: GK-457. USD per unit of `source_currency` at accrual time. None means the
    #: dollar figure beside it was not produced by a reviewed conversion — either
    #: a pre-GK-457 row, or one held at $0 because the currency had no rate — so
    #: the panel shows it rather than presenting the total as settled.
    fx_rate_to_usd: float | None
    amount_usd: float
    status: str
    vests_at: datetime
    vested_at: datetime | None
    paid_at: datetime | None
    payout_batch_id: int | None


class PayoutCommissionsPage(BaseModel):
    items: list[PayoutCommissionOut]
    total: int
    total_amount_usd: float


class RelationshipRow(BaseModel):
    referee_id: int
    referee_username: str | None
    referee_first_name: str | None
    referee_joined_at: datetime
    referrer_id: int
    referrer_username: str | None
    referrer_first_name: str | None
    source: str | None
    attributed_at: datetime | None
    review_status: str | None
    commission_count: int
    accrued_usd: float
    vested_usd: float
    paid_usd: float


class RelationshipsPage(BaseModel):
    items: list[RelationshipRow]
    total: int


class PartnerStatsRow(BaseModel):
    referrer_id: int
    referrer_username: str | None
    referrer_first_name: str | None
    invited_count: int
    paid_count: int
    accrued_usd: float
    pending_usd: float
    available_usd: float
    paid_usd: float


class PartnerStatsPage(BaseModel):
    items: list[PartnerStatsRow]
    total: int


class PayoutBatchOut(BaseModel):
    id: int
    status: str
    currency: str
    threshold_amount: float
    total_amount: float
    commission_count: int
    note: str | None
    created_at: datetime
    sent_at: datetime | None
    paid_at: datetime | None
    cancelled_at: datetime | None
    commissions: list[PayoutCommissionOut] = []


class PayoutBatchesPage(BaseModel):
    items: list[PayoutBatchOut]
    total: int


class PayoutBatchCreate(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
    threshold_usd: float = Field(
        default=float(PAYOUT_THRESHOLD_USD),
        ge=float(PAYOUT_THRESHOLD_USD),
        le=1_000_000,
    )


class PayoutBatchTransition(BaseModel):
    action: str = Field(..., pattern="^(sent|paid|cancelled)$")
    tx_hash: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class PayoutSummary(BaseModel):
    month: str
    generated_at: datetime
    unbatched_vested_count: int
    unbatched_vested_amount_usd: float
    batch_counts: dict[str, int]
    batch_amounts_usd: dict[str, float]
    telegram_text: str


@router.get("/leaderboard", response_model=list[LeaderRow])
async def get_leaderboard(
    db: DB, _: CurrentAdmin, limit: int = Query(20, ge=1, le=200)
):
    rows = await leaderboard(db, limit=limit)
    return [
        LeaderRow(
            user_id=u.id,
            username=u.username,
            first_name=u.first_name,
            referrals=count,
            bonus_days=bonus,
        )
        for u, count, bonus in rows
    ]


@router.get("/payouts/commissions", response_model=PayoutCommissionsPage)
async def list_payout_commissions(
    db: DB,
    _: CurrentAdmin,
    status: str = Query("vested", pattern="^(pending|vested|cancelled|paid)$"),
    batched: bool | None = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> PayoutCommissionsPage:
    referrer = aliased(User)
    referee = aliased(User)
    base = (
        select(ReferralCommission, referrer, referee)
        .join(referrer, referrer.id == ReferralCommission.referrer_id)
        .join(referee, referee.id == ReferralCommission.referee_id)
        .where(ReferralCommission.status == status)
    )
    amount_base = select(ReferralCommission).where(ReferralCommission.status == status)
    if batched is True:
        base = base.where(ReferralCommission.payout_batch_id.is_not(None))
        amount_base = amount_base.where(ReferralCommission.payout_batch_id.is_not(None))
    elif batched is False:
        base = base.where(ReferralCommission.payout_batch_id.is_(None))
        amount_base = amount_base.where(ReferralCommission.payout_batch_id.is_(None))

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0)
    amount_subquery = amount_base.with_only_columns(ReferralCommission.amount_usd).subquery()
    total_amount = (
        await db.execute(
            select(func.coalesce(func.sum(amount_subquery.c.amount_usd), 0))
        )
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(
                ReferralCommission.vested_at.asc().nullslast(),
                ReferralCommission.vests_at.asc(),
                ReferralCommission.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return PayoutCommissionsPage(
        items=[_commission_out(commission, referrer_user, referee_user) for commission, referrer_user, referee_user in rows],
        total=total,
        total_amount_usd=float(total_amount or 0),
    )


@router.get("/payouts/batches", response_model=PayoutBatchesPage)
async def list_payout_batches(
    db: DB,
    _: CurrentAdmin,
    status: str | None = Query(None, pattern="^(draft|sent|paid|cancelled)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> PayoutBatchesPage:
    base = select(ReferralPayoutBatch)
    if status:
        base = base.where(ReferralPayoutBatch.status == status)
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0)
    batches = (
        await db.execute(
            base.order_by(ReferralPayoutBatch.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return PayoutBatchesPage(
        items=[await _batch_out(db, batch, include_commissions=True) for batch in batches],
        total=total,
    )


@router.post("/payouts/batches", response_model=PayoutBatchOut)
async def generate_payout_batch(
    payload: PayoutBatchCreate,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
) -> PayoutBatchOut:
    vested = await vest_due_commissions(db)
    batch = await create_referral_payout_batch(
        db,
        threshold_usd=Decimal(str(payload.threshold_usd)),
        note=(payload.note or "").strip() or None,
    )
    if batch is None:
        raise HTTPException(400, "Vested commissions are below payout threshold")

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="referral_payout_batch.create",
        target_type="referral_payout_batch",
        target_id=batch.id,
        details={
            "vested_now": len(vested),
            "threshold_usd": float(payload.threshold_usd),
            "total_amount": float(batch.total_amount),
            "commission_count": batch.commission_count,
            "note": payload.note or None,
        },
        request=request,
    )
    return await _batch_out(db, batch, include_commissions=True)


@router.post("/payouts/batches/{batch_id}/transition", response_model=PayoutBatchOut)
async def transition_payout_batch(
    batch_id: int,
    payload: PayoutBatchTransition,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
) -> PayoutBatchOut:
    try:
        batch = await transition_referral_payout_batch(
            db,
            batch_id,
            action=payload.action,
            admin_id=admin.id,
            tx_hash=payload.tx_hash,
            note=payload.note,
        )
    except PayoutTransitionError as exc:
        raise HTTPException(400, str(exc)) from exc

    if batch is None:
        raise HTTPException(404, "Payout batch not found")

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action=f"referral_payout_batch.{payload.action}",
        target_type="referral_payout_batch",
        target_id=batch.id,
        details={
            "status": batch.status,
            "tx_hash": (payload.tx_hash or "").strip() or None,
            "note": (payload.note or "").strip() or None,
            "total_amount": float(batch.total_amount),
            "commission_count": batch.commission_count,
        },
        request=request,
    )
    return await _batch_out(db, batch, include_commissions=True)


@router.get("/payouts/summary", response_model=PayoutSummary)
async def get_payout_summary(
    db: DB,
    _: CurrentAdmin,
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
) -> PayoutSummary:
    month_start, month_end = _month_range(month)
    month_label = f"{month_start.year:04d}-{month_start.month:02d}"
    eligible_referrers = (
        select(
            ReferralCommission.referrer_id.label("referrer_id"),
            func.count(ReferralCommission.id).label("commission_count"),
            func.sum(ReferralCommission.amount_usd).label("amount_usd"),
        )
        .where(
            ReferralCommission.status == "vested",
            ReferralCommission.payout_batch_id.is_(None),
        )
        .group_by(ReferralCommission.referrer_id)
        .having(func.sum(ReferralCommission.amount_usd) >= PAYOUT_THRESHOLD_USD)
        .subquery()
    )
    ready_count, ready_amount = (
        await db.execute(
            select(
                func.coalesce(func.sum(eligible_referrers.c.commission_count), 0),
                func.coalesce(func.sum(eligible_referrers.c.amount_usd), 0),
            )
        )
    ).one()

    monthly_batches = (
        await db.execute(
            select(ReferralPayoutBatch).where(
                or_(
                    and_(
                        ReferralPayoutBatch.status == PAYOUT_DRAFT,
                        ReferralPayoutBatch.created_at >= month_start,
                        ReferralPayoutBatch.created_at < month_end,
                    ),
                    and_(
                        ReferralPayoutBatch.status == PAYOUT_SENT,
                        ReferralPayoutBatch.sent_at >= month_start,
                        ReferralPayoutBatch.sent_at < month_end,
                    ),
                    and_(
                        ReferralPayoutBatch.status == PAYOUT_PAID,
                        ReferralPayoutBatch.paid_at >= month_start,
                        ReferralPayoutBatch.paid_at < month_end,
                    ),
                    and_(
                        ReferralPayoutBatch.status == PAYOUT_CANCELLED,
                        ReferralPayoutBatch.cancelled_at >= month_start,
                        ReferralPayoutBatch.cancelled_at < month_end,
                    ),
                )
            )
        )
    ).scalars().all()
    counts = {status: 0 for status in (PAYOUT_DRAFT, PAYOUT_SENT, PAYOUT_PAID, PAYOUT_CANCELLED)}
    amounts = {status: 0.0 for status in (PAYOUT_DRAFT, PAYOUT_SENT, PAYOUT_PAID, PAYOUT_CANCELLED)}
    for batch in monthly_batches:
        if batch.status not in counts:
            continue
        counts[batch.status] += 1
        amounts[batch.status] += float(batch.total_amount or 0)

    generated_at = utcnow()
    ready_count = int(ready_count or 0)
    ready_amount = float(ready_amount or 0)
    return PayoutSummary(
        month=month_label,
        generated_at=generated_at,
        unbatched_vested_count=ready_count,
        unbatched_vested_amount_usd=ready_amount,
        batch_counts=counts,
        batch_amounts_usd=amounts,
        telegram_text=_telegram_summary_text(
            month_label=month_label,
            ready_count=ready_count,
            ready_amount=ready_amount,
            counts=counts,
            amounts=amounts,
        ),
    )


@router.get("/relationships", response_model=RelationshipsPage)
async def list_relationships(
    db: DB,
    _: CurrentAdmin,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> RelationshipsPage:
    """All A -> B referral relationships (who invited whom), paid or not.

    GK-373 (C02/C06/B11): the payouts table only surfaces *vested* commissions,
    so a fresh referral was invisible in admin. This view joins every attributed
    user to their referrer and to a per-pair commission summary so the
    relationship and any money are visible from the first link.
    """
    referrer = aliased(User)
    referee = aliased(User)
    comm = (
        select(
            ReferralCommission.referee_id.label("referee_id"),
            func.count(ReferralCommission.id).label("commission_count"),
            func.coalesce(
                func.sum(
                    case(
                        (ReferralCommission.status != "cancelled", ReferralCommission.amount_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("accrued_usd"),
            func.coalesce(
                func.sum(
                    case(
                        (ReferralCommission.status == "vested", ReferralCommission.amount_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("vested_usd"),
            func.coalesce(
                func.sum(
                    case(
                        (ReferralCommission.status == "paid", ReferralCommission.amount_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("paid_usd"),
        )
        .group_by(ReferralCommission.referee_id)
        .subquery()
    )

    total = int(
        (
            await db.execute(
                select(func.count(referee.id)).where(referee.referrer_id.is_not(None))
            )
        ).scalar_one()
        or 0
    )

    rows = (
        await db.execute(
            select(
                referee,
                referrer,
                ReferralAttribution,
                comm.c.commission_count,
                comm.c.accrued_usd,
                comm.c.vested_usd,
                comm.c.paid_usd,
            )
            .join(referrer, referrer.id == referee.referrer_id)
            .outerjoin(ReferralAttribution, ReferralAttribution.referee_id == referee.id)
            .outerjoin(comm, comm.c.referee_id == referee.id)
            .where(referee.referrer_id.is_not(None))
            .order_by(referee.joined_at.desc(), referee.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        RelationshipRow(
            referee_id=referee_user.id,
            referee_username=referee_user.username,
            referee_first_name=referee_user.first_name,
            referee_joined_at=referee_user.joined_at,
            referrer_id=referrer_user.id,
            referrer_username=referrer_user.username,
            referrer_first_name=referrer_user.first_name,
            source=attribution.source if attribution else None,
            attributed_at=attribution.attributed_at if attribution else None,
            review_status=attribution.review_status if attribution else None,
            commission_count=int(commission_count or 0),
            accrued_usd=float(accrued_usd or 0),
            vested_usd=float(vested_usd or 0),
            paid_usd=float(paid_usd or 0),
        )
        for referee_user, referrer_user, attribution, commission_count, accrued_usd, vested_usd, paid_usd in rows
    ]
    return RelationshipsPage(items=items, total=total)


@router.get("/partners", response_model=PartnerStatsPage)
async def list_partner_stats(
    db: DB,
    _: CurrentAdmin,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> PartnerStatsPage:
    """Per-partner referral stats, visible from the first invite (issue #9).

    Mirrors the bot "💰 Партнёрам" view at admin scale so a partner's numbers are
    never empty before payment+vesting: invited N, paid M, accrued total
    (including still-pending rows), and the confirmed "available to withdraw"
    (vested, unpaid) balance. Read-only aggregation — vesting/economics (20% / 3
    months / $100) are unchanged.
    """
    invited = (
        select(
            User.referrer_id.label("referrer_id"),
            func.count(User.id).label("invited_count"),
        )
        .where(User.referrer_id.is_not(None))
        .group_by(User.referrer_id)
        .subquery()
    )
    comm = (
        select(
            ReferralCommission.referrer_id.label("referrer_id"),
            func.count(
                func.distinct(
                    case(
                        (
                            ReferralCommission.status != "cancelled",
                            ReferralCommission.referee_id,
                        ),
                    )
                )
            ).label("paid_count"),
            func.coalesce(
                func.sum(
                    case(
                        (ReferralCommission.status != "cancelled", ReferralCommission.amount_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("accrued_usd"),
            func.coalesce(
                func.sum(
                    case(
                        (ReferralCommission.status == "pending", ReferralCommission.amount_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("pending_usd"),
            func.coalesce(
                func.sum(
                    case(
                        (ReferralCommission.status == "vested", ReferralCommission.amount_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("available_usd"),
            func.coalesce(
                func.sum(
                    case(
                        (ReferralCommission.status == "paid", ReferralCommission.amount_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("paid_usd"),
        )
        .group_by(ReferralCommission.referrer_id)
        .subquery()
    )

    total = int(
        (await db.execute(select(func.count()).select_from(invited))).scalar_one() or 0
    )

    referrer = aliased(User)
    rows = (
        await db.execute(
            select(
                referrer,
                invited.c.invited_count,
                comm.c.paid_count,
                comm.c.accrued_usd,
                comm.c.pending_usd,
                comm.c.available_usd,
                comm.c.paid_usd,
            )
            .join(referrer, referrer.id == invited.c.referrer_id)
            .outerjoin(comm, comm.c.referrer_id == invited.c.referrer_id)
            .order_by(
                func.coalesce(comm.c.accrued_usd, 0).desc(),
                invited.c.invited_count.desc(),
                referrer.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        PartnerStatsRow(
            referrer_id=referrer_user.id,
            referrer_username=referrer_user.username,
            referrer_first_name=referrer_user.first_name,
            invited_count=int(invited_count or 0),
            paid_count=int(paid_count or 0),
            accrued_usd=float(accrued_usd or 0),
            pending_usd=float(pending_usd or 0),
            available_usd=float(available_usd or 0),
            paid_usd=float(paid_usd or 0),
        )
        for (
            referrer_user,
            invited_count,
            paid_count,
            accrued_usd,
            pending_usd,
            available_usd,
            paid_usd,
        ) in rows
    ]
    return PartnerStatsPage(items=items, total=total)


@router.get("/suspicious", response_model=list[SuspiciousAttributionRow])
async def get_suspicious_attributions(
    db: DB,
    _: CurrentAdmin,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    referrer = aliased(User)
    referee = aliased(User)
    ignored_referrer = aliased(User)
    rows = (
        await db.execute(
            select(ReferralAttribution, referrer, referee, ignored_referrer)
            .join(referrer, referrer.id == ReferralAttribution.referrer_id)
            .join(referee, referee.id == ReferralAttribution.referee_id)
            .outerjoin(
                ignored_referrer,
                ignored_referrer.id == ReferralAttribution.last_ignored_referrer_id,
            )
            .where(ReferralAttribution.review_status == "suspicious")
            .order_by(ReferralAttribution.last_ignored_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        SuspiciousAttributionRow(
            attribution_id=attribution.id,
            referrer_id=attribution.referrer_id,
            referrer_username=referrer_user.username,
            referee_id=attribution.referee_id,
            referee_username=referee_user.username,
            source=attribution.source,
            code=attribution.code,
            attributed_at=attribution.attributed_at,
            review_status=attribution.review_status,
            suspicious_reason=attribution.suspicious_reason,
            ignored_attempt_count=attribution.ignored_attempt_count,
            last_ignored_referrer_id=attribution.last_ignored_referrer_id,
            last_ignored_referrer_username=ignored_user.username if ignored_user else None,
            last_ignored_source=attribution.last_ignored_source,
            last_ignored_code=attribution.last_ignored_code,
            last_ignored_at=attribution.last_ignored_at,
        )
        for attribution, referrer_user, referee_user, ignored_user in rows
    ]


@router.get("/tree/{user_id}", response_model=TreeNode)
async def get_tree(
    user_id: int,
    db: DB,
    _: CurrentAdmin,
    depth: int = Query(2, ge=0, le=5),
):
    root = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not root:
        raise HTTPException(404, "User not found")

    visited = 0

    async def build(u: User, level: int) -> TreeNode:
        nonlocal visited
        visited += 1
        if visited > _MAX_TREE_NODES:
            return TreeNode(user_id=u.id, username=u.username, joined_at=u.joined_at, children=[])
        node = TreeNode(user_id=u.id, username=u.username, joined_at=u.joined_at, children=[])
        if level <= 0:
            return node
        kids = (await db.execute(select(User).where(User.referrer_id == u.id))).scalars().all()
        for k in kids:
            if visited > _MAX_TREE_NODES:
                break
            node.children.append(await build(k, level - 1))
        return node

    return await build(root, depth)


async def _batch_out(
    db: DB,
    batch: ReferralPayoutBatch,
    *,
    include_commissions: bool = False,
) -> PayoutBatchOut:
    commissions: list[PayoutCommissionOut] = []
    if include_commissions:
        referrer = aliased(User)
        referee = aliased(User)
        rows = (
            await db.execute(
                select(ReferralCommission, referrer, referee)
                .join(referrer, referrer.id == ReferralCommission.referrer_id)
                .join(referee, referee.id == ReferralCommission.referee_id)
                .where(ReferralCommission.payout_batch_id == batch.id)
                .order_by(ReferralCommission.vested_at.asc().nullslast(), ReferralCommission.id.asc())
            )
        ).all()
        commissions = [
            _commission_out(commission, referrer_user, referee_user)
            for commission, referrer_user, referee_user in rows
        ]

    return PayoutBatchOut(
        id=batch.id,
        status=batch.status,
        currency=batch.currency,
        threshold_amount=float(batch.threshold_amount),
        total_amount=float(batch.total_amount),
        commission_count=batch.commission_count,
        note=batch.note,
        created_at=batch.created_at,
        sent_at=batch.sent_at,
        paid_at=batch.paid_at,
        cancelled_at=batch.cancelled_at,
        commissions=commissions,
    )


def _commission_out(
    commission: ReferralCommission,
    referrer: User,
    referee: User,
) -> PayoutCommissionOut:
    return PayoutCommissionOut(
        id=commission.id,
        referrer_id=commission.referrer_id,
        referrer_username=referrer.username,
        referrer_first_name=referrer.first_name,
        referee_id=commission.referee_id,
        referee_username=referee.username,
        referee_first_name=referee.first_name,
        source_provider=commission.source_provider,
        source_invoice_id=commission.source_invoice_id,
        source_amount=float(commission.source_amount),
        source_currency=commission.source_currency,
        fx_rate_to_usd=(
            float(commission.fx_rate_to_usd)
            if commission.fx_rate_to_usd is not None
            else None
        ),
        amount_usd=float(commission.amount_usd),
        status=commission.status,
        vests_at=commission.vests_at,
        vested_at=commission.vested_at,
        paid_at=commission.paid_at,
        payout_batch_id=commission.payout_batch_id,
    )


def _month_range(month: str | None) -> tuple[datetime, datetime]:
    now = utcnow()
    if month:
        year, month_number = (int(part) for part in month.split("-", 1))
    else:
        year, month_number = now.year, now.month
    start = datetime(year, month_number, 1, tzinfo=UTC)
    if month_number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month_number + 1, 1, tzinfo=UTC)
    return start, end


def _telegram_summary_text(
    *,
    month_label: str,
    ready_count: int,
    ready_amount: float,
    counts: dict[str, int],
    amounts: dict[str, float],
) -> str:
    # BLK-005 / GK-100: operator copy for the monthly support payout process.
    # Grant's pre-final spec: payouts run monthly through support on the 1st-5th,
    # processing takes 3-5 days, and the accrual threshold is $100.
    return "\n".join(
        [
            f"Реферальные выплаты: {month_label}",
            f"Готово к формированию: {ready_count} комиссий / ${ready_amount:.2f}",
            f"Черновик: {counts[PAYOUT_DRAFT]} batch / ${amounts[PAYOUT_DRAFT]:.2f}",
            f"Отправлено: {counts[PAYOUT_SENT]} batch / ${amounts[PAYOUT_SENT]:.2f}",
            f"Выплачено: {counts[PAYOUT_PAID]} batch / ${amounts[PAYOUT_PAID]:.2f}",
            f"Отменено: {counts[PAYOUT_CANCELLED]} batch / ${amounts[PAYOUT_CANCELLED]:.2f}",
            "",
            f"Порог выплаты: ${float(PAYOUT_THRESHOLD_USD):.2f}. "
            "Выплаты — ежемесячно через поддержку с 1 по 5 число, обработка 3–5 дней.",
        ]
    )
