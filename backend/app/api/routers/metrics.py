from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DB, CurrentAdmin
from app.config import get_settings
from app.db.models import Payment, Subscription, User
from app.services.backup_verification import assess, latest_verification
from app.services.subscription_cancellation import open_manual_cancellations_query

router = APIRouter(prefix="/metrics", tags=["metrics"])

_RECOGNIZED_PAYMENT_STATUSES = ("succeeded", "refunded")


class CurrencyAmount(BaseModel):
    currency: str
    amount: float


class BackupVerificationStatus(BaseModel):
    """GK-437. `state` is one of ok / never / stale / failed — see
    `app.services.backup_verification`. The three failure states are kept
    distinct because they call for different actions: deploy the canary,
    restart it, or go and fix the backups."""

    state: str
    verified_at: datetime | None
    age_hours: float | None
    dump_file: str | None
    detail: str | None
    headline: str


class MetricSummary(BaseModel):
    active_subs: int
    # GK-483: the client's own team — Grant, Owner's two accounts, curators and
    # moderators. They hold real access and are deliberately NOT in `active_subs`,
    # so the panel shows both numbers rather than one number that is quietly two
    # different things. Grant asked to stay visible, just not counted as paying.
    comp_subs: int
    new_users_7d: int
    # Per-currency revenue: RUB (Lava) and USD (Stripe/manual/USDT) are NEVER summed
    # together — a RUB amount is not a USD amount. See GK-414.
    revenue_30d: list[CurrencyAmount]
    revenue_7d: list[CurrencyAmount]
    # USD-only scalars kept for backward compatibility with existing API readers.
    mrr_usd: float
    revenue_30d_usd: float
    revenue_7d_usd: float
    awaiting_review: int
    # GK-433: members who asked to stop being charged and whose card is, as far
    # as we know, still live at the provider. Anything above zero is somebody
    # about to be charged against their wishes, so it belongs on the first
    # screen an admin sees rather than one page deeper.
    manual_cancellations_open: int
    churn_30d_pct: float
    total_users: int
    # GK-437: "the backup job succeeded" is not the claim that matters; "the
    # newest backup was restorable as of X" is. Surfaced here so the answer is
    # a fact on the dashboard rather than something someone has to go and ask.
    backup_verification: BackupVerificationStatus


class RevenuePoint(BaseModel):
    date: str
    amount: float


def _active_subs_query(now: datetime):
    """Paying members with access right now. GK-483: comp rows are excluded here
    and counted separately — this is the figure Grant asked the team to stay out
    of, and it is read by the dashboard tile and by churn's denominator."""
    return select(func.count(Subscription.id)).where(
        Subscription.status == "active",
        Subscription.expires_at > now,
        Subscription.is_comp.is_(False),
    )


def _comp_subs_query():
    """The team. No date filter on purpose: a comp row's access does not run out
    (`app.services.subscription.is_comp_access`), so an `expires_at` in the past
    is normal for one and must not make it disappear from the panel."""
    return select(func.count(Subscription.id)).where(
        Subscription.status == "active",
        Subscription.is_comp.is_(True),
    )


def _expired_subs_query(since: datetime):
    """Churn's numerator. Excludes comp for the same reason the denominator does:
    mixing the two makes the churn rate a ratio of two different populations."""
    return select(func.count(Subscription.id)).where(
        Subscription.status == "expired",
        Subscription.expires_at >= since,
        Subscription.is_comp.is_(False),
    )


def _recognized_revenue_sum():
    """Net confirmed cash: gross payment less confirmed refund total only."""
    return func.coalesce(
        func.sum(Payment.amount - func.coalesce(Payment.refunded_amount, 0)),
        0,
    )


def _recognized_revenue_query(since: datetime):
    return select(_recognized_revenue_sum()).where(
        Payment.status.in_(_RECOGNIZED_PAYMENT_STATUSES),
        Payment.created_at >= since,
    )


def _recognized_revenue_by_currency_query(since: datetime):
    """Net confirmed cash grouped by currency — the honest, un-mixed breakdown."""
    return (
        select(
            Payment.currency.label("currency"),
            _recognized_revenue_sum().label("amount"),
        )
        .where(
            Payment.status.in_(_RECOGNIZED_PAYMENT_STATUSES),
            Payment.created_at >= since,
        )
        .group_by(Payment.currency)
        .order_by(Payment.currency)
    )


def _recognized_revenue_by_day_query(since: datetime, currency: str | None = None):
    q = (
        select(
            func.date_trunc("day", Payment.created_at).label("d"),
            _recognized_revenue_sum().label("amount"),
        )
        .where(
            Payment.status.in_(_RECOGNIZED_PAYMENT_STATUSES),
            Payment.created_at >= since,
        )
        .group_by("d")
        .order_by("d")
    )
    if currency is not None:
        q = q.where(Payment.currency == currency)
    return q


async def _revenue_by_currency(db, since: datetime) -> list[CurrencyAmount]:
    rows = (await db.execute(_recognized_revenue_by_currency_query(since))).all()
    return [
        CurrencyAmount(currency=(r.currency or "USD"), amount=round(float(r.amount or 0), 2))
        for r in rows
        if float(r.amount or 0) != 0
    ]


def _usd_amount(items: list[CurrencyAmount]) -> float:
    return next((i.amount for i in items if i.currency == "USD"), 0.0)


@router.get("/summary", response_model=MetricSummary)
async def summary(db: DB, _: CurrentAdmin):
    now = datetime.now(UTC)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    total_users = int((await db.execute(select(func.count(User.id)))).scalar_one() or 0)
    new_users_7d = int((await db.execute(select(func.count(User.id)).where(User.joined_at >= d7))).scalar_one() or 0)
    active_subs = int((await db.execute(_active_subs_query(now))).scalar_one() or 0)
    comp_subs = int((await db.execute(_comp_subs_query())).scalar_one() or 0)

    revenue_30d = await _revenue_by_currency(db, d30)
    revenue_7d = await _revenue_by_currency(db, d7)

    awaiting = int(
        (
            await db.execute(
                select(func.count(Payment.id)).where(Payment.status == "awaiting_review")
            )
        ).scalar_one()
        or 0
    )

    # Counted through the same query the queue and the daily alert use, so the
    # three can never disagree with each other.
    manual_cancellations_open = int(
        (
            await db.execute(
                select(func.count()).select_from(open_manual_cancellations_query().subquery())
            )
        ).scalar_one()
        or 0
    )

    expired_30 = int((await db.execute(_expired_subs_query(d30))).scalar_one() or 0)
    base_30 = active_subs + expired_30
    churn = (expired_30 / base_30 * 100.0) if base_30 else 0.0

    # USD-only proxy: revenue last 30d in USD (RUB is reported separately, not mixed in).
    revenue_30d_usd = _usd_amount(revenue_30d)

    backup_health = assess(
        await latest_verification(db),
        now=now,
        max_age_hours=get_settings().backup_verification_max_age_hours,
    )

    return MetricSummary(
        active_subs=active_subs,
        comp_subs=comp_subs,
        new_users_7d=new_users_7d,
        revenue_30d=revenue_30d,
        revenue_7d=revenue_7d,
        mrr_usd=round(revenue_30d_usd, 2),
        revenue_30d_usd=round(revenue_30d_usd, 2),
        revenue_7d_usd=round(_usd_amount(revenue_7d), 2),
        awaiting_review=awaiting,
        manual_cancellations_open=manual_cancellations_open,
        churn_30d_pct=round(churn, 1),
        total_users=total_users,
        backup_verification=BackupVerificationStatus(
            state=backup_health.state,
            verified_at=backup_health.verified_at,
            age_hours=(
                None if backup_health.age_hours is None else round(backup_health.age_hours, 1)
            ),
            dump_file=backup_health.dump_file,
            detail=backup_health.detail,
            headline=backup_health.headline,
        ),
    )


@router.get("/revenue", response_model=list[RevenuePoint])
async def revenue(db: DB, _: CurrentAdmin, days: int = 30, currency: str = "USD"):
    # Single-currency daily series so the chart never mixes RUB and USD (GK-414).
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    q = _recognized_revenue_by_day_query(since, currency=currency)
    rows = (await db.execute(q)).all()
    by_day = {r.d.date().isoformat(): float(r.amount) for r in rows}
    points: list[RevenuePoint] = []
    for i in range(days):
        d = (since + timedelta(days=i)).date().isoformat()
        points.append(RevenuePoint(date=d, amount=round(by_day.get(d, 0.0), 2)))
    return points
