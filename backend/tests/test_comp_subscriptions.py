"""GK-483: the team keeps access and stays out of the paying figures.

Grant, 2026-08-23: «Нас оставь и в чате, и в админ-панели, но отдельным статусом,
не как платящих». No status could carry that — `active` counts as paying,
`gift` means somebody bought it, and a fourth `sub_status` value would fall out
of `ACCESS_HOLDING_STATUSES` and so mean "no access to revoke", the opposite of
«оставь в чате». `subscriptions.is_comp` is a flag beside the status instead.

Two properties have to hold together, and these tests pin both:

* a flagged row's access never runs out, so the hourly `kick_expired_job`
  cannot reach it (the alternative — a far-future `expires_at` — is a deadline
  that eventually arrives, and reads like a paid subscription until it does);
* every count that means "paying members" filters it out, while the flag itself
  cannot move a revenue figure, because revenue is computed from `payments` and
  a comp row has none.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.api.routers.metrics import (
    _active_subs_query,
    _comp_subs_query,
    _expired_subs_query,
    _recognized_revenue_by_currency_query,
    _recognized_revenue_by_day_query,
    _recognized_revenue_query,
)
from app.services.google_sheets_export import CRM_EXPORT_SPECS
from app.services.subscription import (
    expire_subscriptions,
    has_subscription_access,
    is_comp_access,
    is_manual_usdt_expiry_reminder_due,
    should_revoke_access,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def make_subscription(**overrides):
    """A row whose access window closed a month ago — the shape GK-484 found on
    the live host for Grant himself (`sub#2`, window ended 2026-07-10)."""
    data = {
        "id": 2,
        "status": "active",
        "source": "manual",
        "provider": None,
        "provider_subscription_id": None,
        "provider_status": None,
        "current_period_end": None,
        "expires_at": NOW - timedelta(days=45),
        "is_comp": False,
        "notified_expiring": False,
        "grace_started_at": None,
        "grace_ends_at": None,
        "cancel_at_period_end": False,
        "access_revoke_attempted_at": None,
        "access_revoked_at": None,
        "access_revoke_retry_after_at": None,
        "access_revoke_attempts": 0,
        "access_revoke_error": None,
        "access_revoke_abandoned_at": None,
        "invite_link": None,
        "user": SimpleNamespace(tg_id=556081290),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _sql(query) -> str:
    return str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


# --------------------------------------------------------------------------
# The access rule
# --------------------------------------------------------------------------


def test_comp_row_keeps_access_long_after_its_date_passed():
    sub = make_subscription(is_comp=True)

    assert is_comp_access(sub) is True
    assert has_subscription_access(sub, NOW) is True


def test_the_same_row_without_the_flag_has_no_access():
    # The only difference between this and the test above is the flag, so the
    # flag is doing the work and not some other property of the fixture.
    assert has_subscription_access(make_subscription(), NOW) is False


def test_comp_row_is_never_due_for_removal():
    """This is the GK-484 predicate. `kick_expired_job` bans a member out of the
    production channel and then DMs them a subscription pitch; the client's own
    manager must never be in the set it processes."""
    assert should_revoke_access(make_subscription(is_comp=True), NOW) is False
    # ... whereas the unflagged row is exactly what fires that job today.
    assert should_revoke_access(make_subscription(), NOW) is True


def test_clearing_the_flag_makes_an_overdue_row_due_again():
    """Leaving the team is not a special case: the access window that was there
    all along starts applying again the moment the mark comes off."""
    sub = make_subscription(is_comp=True)
    assert should_revoke_access(sub, NOW) is False

    sub.is_comp = False

    assert should_revoke_access(sub, NOW) is True


def test_banning_a_team_member_still_removes_them():
    """`access_revoked_at` is checked before the flag on purpose — a ban is an
    admin decision about a person, not a statement about how they were billed."""
    sub = make_subscription(is_comp=True, access_revoked_at=NOW - timedelta(hours=1))

    assert has_subscription_access(sub, NOW) is False


def test_flag_does_not_resurrect_a_row_somebody_moved_to_expired():
    sub = make_subscription(is_comp=True, status="expired")

    assert has_subscription_access(sub, NOW) is False


def test_comp_member_is_never_nudged_to_renew():
    """The daily reminder sells a renewal. Nobody bought this one."""
    due = make_subscription(
        source="usdt",
        expires_at=NOW + timedelta(days=2),
    )
    assert is_manual_usdt_expiry_reminder_due(due, now=NOW) is True

    due.is_comp = True
    assert is_manual_usdt_expiry_reminder_due(due, now=NOW) is False


@pytest.mark.asyncio
async def test_expiry_sweep_does_not_even_load_comp_rows():
    """Belt and braces: `should_revoke_access` already refuses, but excluding the
    flag in the query too means the job's own SELECT and GK-484's launch-gate
    query describe the same set instead of two sets that agree by luck."""

    class RecordingSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    session = RecordingSession()
    assert await expire_subscriptions(session) == []

    sql = _sql(session.statements[0])
    assert "subscriptions.is_comp is false" in sql


# --------------------------------------------------------------------------
# The figures
# --------------------------------------------------------------------------


def test_active_subs_excludes_the_team():
    sql = _sql(_active_subs_query(NOW))

    assert "count(subscriptions.id)" in sql
    assert "subscriptions.status = 'active'" in sql
    assert "subscriptions.is_comp is false" in sql


def test_team_is_counted_separately_and_without_a_date_filter():
    """A comp row's `expires_at` is routinely in the past — that is the whole
    point — so a date filter here would hide exactly the members Grant asked to
    stay visible in the panel."""
    sql = _sql(_comp_subs_query())

    assert "subscriptions.is_comp is true" in sql
    assert "subscriptions.expires_at" not in sql


def test_churn_numerator_excludes_the_team_too():
    """Denominator (`active_subs`) and numerator have to describe the same
    population, or the percentage is a ratio between two different groups."""
    sql = _sql(_expired_subs_query(NOW - timedelta(days=30)))

    assert "subscriptions.status = 'expired'" in sql
    assert "subscriptions.is_comp is false" in sql


@pytest.mark.parametrize(
    "query",
    [
        _recognized_revenue_query(NOW - timedelta(days=30)),
        _recognized_revenue_by_currency_query(NOW - timedelta(days=30)),
        _recognized_revenue_by_day_query(NOW - timedelta(days=30)),
    ],
)
def test_revenue_cannot_move_when_a_comp_row_is_added(query):
    """Recognized revenue reads `payments` and nothing else. A comp subscription
    has no payment behind it, so adding one is provably unable to change any
    revenue figure — no exclusion clause needed, and none must creep in."""
    sql = _sql(query)

    assert "from payments" in sql
    assert "subscriptions" not in sql


def test_crm_export_distinguishes_the_team():
    """Without this column the sheet reproduces exactly the inflation the panel
    now avoids, for anyone counting subscription rows in the CRM."""
    spec = next(s for s in CRM_EXPORT_SPECS if s.name == "Subscriptions")

    assert "is_comp" in spec.headers
    # Appended last on purpose: rows for subscriptions that have since left the
    # database are never rewritten, so inserting a column mid-row would shift
    # those stale rows out of alignment with their own headers.
    assert spec.headers[-1] == "is_comp"
