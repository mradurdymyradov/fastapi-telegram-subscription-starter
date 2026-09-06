"""GK-483 acceptance check: adding a comp row must move no paying figure.

Reads the panel's OWN expressions — the query builders in
`app.api.routers.metrics` and the churn arithmetic copied from `summary()` — so
this measures what the dashboard shows rather than a re-implementation of it,
and then puts the same row through the access predicates the hourly removal job
uses. Read-only in effect: it inserts one flagged subscription inside a
transaction, measures, and rolls the insert back, then re-measures from a fresh
session to prove nothing was written.

Kept rather than thrown away because the acceptance criteria ask for the numbers
measured before and after on the live database, and that can only happen once
this migration is deployed there — this is the thing to run then.

    docker exec -i membership_saas-api-1 python -m app.ops.comp_status_check
"""
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.api.routers.metrics import (
    _active_subs_query,
    _comp_subs_query,
    _expired_subs_query,
    _recognized_revenue_by_currency_query,
)
from app.db.models import Plan, Subscription, User
from app.db.session import async_session
from app.services.subscription import (
    expire_subscriptions,
    has_subscription_access,
    should_revoke_access,
)

#: A window wide enough that churn and revenue are not trivially zero on a
#: database whose activity predates the last month. On the live host the 30-day
#: figures are the ones the dashboard shows; both are measured, because "the
#: number did not move" says nothing when the number was zero to begin with.
ALL_TIME = datetime(2000, 1, 1, tzinfo=UTC)


async def _churn(db, active, since):
    expired = int((await db.execute(_expired_subs_query(since))).scalar_one() or 0)
    base = active + expired
    return expired, round((expired / base * 100.0) if base else 0.0, 1)


async def _revenue(db, since):
    return {
        (r.currency or "USD"): round(float(r.amount or 0), 2)
        for r in (await db.execute(_recognized_revenue_by_currency_query(since))).all()
    }


async def measure(db, now):
    d30 = now - timedelta(days=30)
    active = int((await db.execute(_active_subs_query(now))).scalar_one() or 0)
    comp = int((await db.execute(_comp_subs_query())).scalar_one() or 0)
    expired_30, churn_30 = await _churn(db, active, d30)
    expired_all, churn_all = await _churn(db, active, ALL_TIME)
    return {
        "active_subs": active,
        "comp_subs": comp,
        "expired_30d": expired_30,
        "churn_30d_pct": churn_30,
        "expired_all": expired_all,
        "churn_all_pct": churn_all,
        "revenue_30d": await _revenue(db, d30),
        "revenue_all": await _revenue(db, ALL_TIME),
    }


def show(label, m):
    print(
        f"{label:>11}: active_subs={m['active_subs']}  comp_subs={m['comp_subs']}\n"
        f"{'':>13}30d : expired={m['expired_30d']}  churn={m['churn_30d_pct']}%  "
        f"revenue={m['revenue_30d']}\n"
        f"{'':>13}all: expired={m['expired_all']}  churn={m['churn_all_pct']}%  "
        f"revenue={m['revenue_all']}"
    )


async def main():
    now = datetime.now(UTC)
    async with async_session() as db:
        user = (await db.execute(select(User).order_by(User.id).limit(1))).scalar_one()
        plan = (await db.execute(select(Plan).order_by(Plan.id).limit(1))).scalar_one()

        before = await measure(db, now)
        show("before", before)

        # The exact shape GK-484 found for Grant: an access window that closed
        # weeks ago. Unflagged, this row is due for removal by the hourly job and
        # would be counted nowhere; flagged, it must hold access and count nowhere
        # except the team tile.
        db.add(
            Subscription(
                user_id=user.id,
                plan_id=plan.id,
                status="active",
                source="comp",
                is_comp=True,
                started_at=now - timedelta(days=120),
                expires_at=now - timedelta(days=45),
            )
        )
        await db.flush()

        after = await measure(db, now)
        show("after", after)

        for key in (
            "active_subs",
            "expired_30d",
            "churn_30d_pct",
            "expired_all",
            "churn_all_pct",
            "revenue_30d",
            "revenue_all",
        ):
            assert after[key] == before[key], f"{key} moved: {before[key]} -> {after[key]}"
        assert after["comp_subs"] == before["comp_subs"] + 1, "team tile did not move"

        # The same row, seen by the access machinery.
        sub = (
            await db.execute(
                select(Subscription).where(Subscription.is_comp.is_(True))
            )
        ).scalars().all()[-1]
        print(f"\n   comp row: id={sub.id} status={sub.status} "
              f"expires_at={sub.expires_at:%Y-%m-%d} (window closed 45 days ago)")
        print(f"   has_subscription_access = {has_subscription_access(sub, now)}  (expected True)")
        print(f"   should_revoke_access    = {should_revoke_access(sub, now)}  (expected False)")
        assert has_subscription_access(sub, now) is True
        assert should_revoke_access(sub, now) is False

        due = await expire_subscriptions(db)
        print(f"   kick_expired_job would process {len(due)} row(s); "
              f"comp row included: {any(s.id == sub.id for s in due)}  (expected False)")
        assert all(s.id != sub.id for s in due)

        await db.rollback()

    # Re-open a clean session to prove nothing was written.
    async with async_session() as db:
        again = await measure(db, now)
        show("rolled back", again)
        assert again == before, "the measurement changed the database"

    # Say what was NOT proven here. A figure that was zero before cannot be
    # observed to stay zero for the right reason, and reporting it as if it
    # could is how a green run comes to mean less than it looks like it means.
    vacuous = [
        name
        for name, value in (
            ("churn (no rows in `expired` status at all)", before["expired_all"]),
            ("revenue/30d (no payments in the last 30 days)", sum(before["revenue_30d"].values())),
        )
        if not value
    ]
    if vacuous:
        print("\nNot proven by this run, because the figure was already empty:")
        for name in vacuous:
            print(f"  - {name}")
        print("  The SQL-level exclusion for these is pinned in tests/test_comp_subscriptions.py.")

    print("\nOK — every paying figure unchanged, team counted separately, nothing written.")


if __name__ == "__main__":
    asyncio.run(main())
