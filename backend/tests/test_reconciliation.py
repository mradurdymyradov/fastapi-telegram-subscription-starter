from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.db.models import ReconciliationItem, ReconciliationRun
from app.payments.lava_provider import RemoteSale
from app.services import reconciliation
from app.services.reconciliation import (
    LAVA_REMOTE_LOOKBACK_DAYS,
    ReconciliationIssue,
    compare_lava_remote,
    compare_stripe_remote,
    current_provider_subscription_rows,
    reconciliation_summary_text,
    refund_consistency_issues,
    run_reconciliation,
    scan_lava_local,
    scan_stripe_local,
    scan_usdt_local,
)
from app.services.subscription import create_or_extend_subscription

NOW = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self, prior_items=()):
        self.added = []
        self.flushes = 0
        #: Rows a `load_condition_history` lookup would return — one per condition.
        self.prior_items = list(prior_items)

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, _query):
        rows = list(self.prior_items)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def flush(self):
        self.flushes += 1
        for i, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = i


def make_prior_item(**overrides):
    """A row from an earlier run, as `load_condition_history` returns it."""
    data = {
        "id": 500,
        "run_id": 61,
        "provider": "stripe",
        "issue_type": "stripe_paid_invoice_not_succeeded",
        "entity_type": "payment",
        "entity_id": "123",
        "status": "open",
        "resolve_note": None,
        "resolved_by_admin_id": None,
        "resolved_at": None,
        "created_at": NOW - timedelta(days=30),
        "first_seen_at": NOW - timedelta(days=60),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_payment(**overrides):
    data = {
        "id": 10,
        "provider": "stripe",
        "status": "pending",
        "amount": Decimal("19.00"),
        "currency": "USD",
        "external_id": None,
        "stripe_checkout_session_id": None,
        "stripe_invoice_id": None,
        "lava_invoice_id": None,
        "lava_subscription_id": None,
        "provider_event_id": None,
        "tx_hash": None,
        "tx_network": None,
        "tx_confirmed_at": None,
        "created_at": NOW - timedelta(hours=2),
        "approved_at": None,
        # GK-415 reads both: `user_id` to ask whether the sale was fulfilled, and
        # `note`, which is where the Lava buyer email is kept (GK-377).
        "user_id": 3,
        "note": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_subscription(**overrides):
    """A Stripe subscription row with a live provider link, by default access-holding."""
    data = {
        "id": 1,
        "user_id": 3,
        "plan_id": 2,
        "status": "active",
        "source": "stripe",
        "provider": "stripe",
        "provider_subscription_id": "sub_live_1",
        "provider_status": "active",
        "started_at": NOW - timedelta(days=10),
        "expires_at": NOW + timedelta(days=20),
        "current_period_start": NOW - timedelta(days=10),
        "current_period_end": NOW + timedelta(days=20),
        "grace_started_at": None,
        "grace_ends_at": None,
        "cancel_at_period_end": False,
        "access_revoked_at": None,
        "access_revoke_retry_after_at": None,
        "access_revoke_error": None,
        "invite_link": None,
        "notified_expiring": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_event(**overrides):
    data = {
        "id": 20,
        "provider": "stripe",
        "event_id": "evt_123",
        "event_type": "invoice.paid",
        "payment_id": None,
        "processed_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_run_reconciliation_creates_expected_discrepancy_items():
    async def stripe_collector(_session, _now):
        return [
            ReconciliationIssue(
                provider="stripe",
                severity="critical",
                issue_type="stripe_paid_invoice_not_succeeded",
                entity_type="payment",
                entity_id=123,
                external_id="in_123",
                title="Stripe paid invoice mismatch",
                description="Mocked discrepancy",
                expected_state={"payment.status": "succeeded"},
                observed_state={"payment.status": "pending"},
            )
        ]

    async def empty_collector(_session, _now):
        return []

    session = FakeSession()
    run = await run_reconciliation(
        session,
        providers=["stripe", "usdt"],
        triggered_by="test",
        now=NOW,
        collectors={"stripe": stripe_collector, "usdt": empty_collector},
    )

    items = [obj for obj in session.added if isinstance(obj, ReconciliationItem)]
    runs = [obj for obj in session.added if isinstance(obj, ReconciliationRun)]

    assert runs == [run]
    assert run.status == "completed"
    assert run.provider_scope == "stripe,usdt"
    assert run.items_count == 1
    assert run.open_items_count == 1
    assert run.summary["by_provider"]["stripe"] == 1
    assert len(items) == 1
    assert items[0].run_id == run.id
    assert items[0].issue_type == "stripe_paid_invoice_not_succeeded"
    assert items[0].status == "open"


def test_stripe_scan_uses_invoice_events_not_checkout_only_success():
    checkout_only_payment = make_payment(
        id=123,
        status="succeeded",
        stripe_checkout_session_id="cs_123",
        stripe_invoice_id=None,
        external_id="sub_123",
    )
    invoice_event = make_event(payment_id=124, event_id="evt_invoice_paid")
    linked_pending_payment = make_payment(
        id=124,
        status="pending",
        stripe_invoice_id="in_124",
        provider_event_id="evt_invoice_paid",
    )

    issues = scan_stripe_local(
        [checkout_only_payment, linked_pending_payment],
        [],
        [invoice_event],
        NOW,
    )

    issue_types = {issue.issue_type for issue in issues}
    assert "stripe_succeeded_missing_invoice" in issue_types
    assert "stripe_paid_invoice_not_succeeded" in issue_types


def test_usdt_scan_flags_duplicate_tx_and_missing_verification_state():
    first = make_payment(
        id=1,
        provider="usdt",
        status="succeeded",
        tx_network="TRC20",
        tx_hash="0x" + "a" * 64,
        tx_confirmed_at=None,
    )
    second = make_payment(
        id=2,
        provider="usdt",
        status="awaiting_review",
        tx_network="TRC20",
        tx_hash="a" * 64,
        created_at=NOW - timedelta(days=2),
    )

    issues = scan_usdt_local([first, second], NOW)

    issue_types = {issue.issue_type for issue in issues}
    assert "usdt_succeeded_missing_verification" in issue_types
    assert "usdt_duplicate_tx_claim" in issue_types
    assert "usdt_stale_claim_waiting_review" in issue_types


def test_refund_consistency_flags_full_refund_status_mismatch():
    payment = make_payment(
        id=30,
        status="succeeded",
        amount=Decimal("19.00"),
        refunded_amount=Decimal("19.00"),
    )
    issues = refund_consistency_issues("stripe", [payment])
    assert {i.issue_type for i in issues} == {"stripe_full_refund_status_mismatch"}


def test_refund_consistency_flags_partial_refund_status_mismatch():
    payment = make_payment(
        id=31,
        status="refunded",
        amount=Decimal("19.00"),
        refunded_amount=Decimal("9.50"),
    )
    issues = refund_consistency_issues("lava", [payment])
    assert {i.issue_type for i in issues} == {"lava_partial_refund_status_mismatch"}


def test_refund_consistency_flags_refund_exceeds_payment():
    payment = make_payment(
        id=32,
        status="refunded",
        amount=Decimal("19.00"),
        refunded_amount=Decimal("25.00"),
    )
    issues = refund_consistency_issues("usdt", [payment])
    assert {i.issue_type for i in issues} == {"usdt_refund_exceeds_payment"}


def test_refund_consistency_flags_refunded_status_without_amount():
    payment = make_payment(
        id=33,
        status="refunded",
        amount=Decimal("19.00"),
        refunded_amount=Decimal("0"),
    )
    issues = refund_consistency_issues("stripe", [payment])
    assert {i.issue_type for i in issues} == {"stripe_refunded_without_refund_record"}


def test_refund_consistency_clean_for_consistent_states():
    full = make_payment(id=34, status="refunded", amount=Decimal("19.00"), refunded_amount=Decimal("19.00"))
    partial = make_payment(id=35, status="succeeded", amount=Decimal("19.00"), refunded_amount=Decimal("9.50"))
    untouched = make_payment(id=36, status="succeeded", amount=Decimal("19.00"), refunded_amount=Decimal("0"))
    issues = refund_consistency_issues("stripe", [full, partial, untouched])
    assert issues == []


def test_refund_consistency_surfaces_pending_manual_and_failed_states():
    refunds = [
        SimpleNamespace(
            id=1,
            status="pending",
            amount=Decimal("5.00"),
            provider_refund_id="re_pending",
            provider_status="pending",
            failure_reason=None,
            accounting_applied_at=None,
        ),
        SimpleNamespace(
            id=2,
            status="manual_action_required",
            amount=Decimal("4.00"),
            provider_refund_id=None,
            provider_status="manual_action_required",
            failure_reason=None,
            accounting_applied_at=None,
        ),
        SimpleNamespace(
            id=3,
            status="failed",
            amount=Decimal("3.00"),
            provider_refund_id="re_failed",
            provider_status="failed",
            failure_reason="provider rejected",
            accounting_applied_at=None,
        ),
    ]
    payment = make_payment(
        id=37,
        status="succeeded",
        amount=Decimal("19.00"),
        refunded_amount=Decimal("0"),
        refunds=refunds,
    )

    issue_types = {issue.issue_type for issue in refund_consistency_issues("stripe", [payment])}
    assert issue_types == {
        "stripe_refund_pending",
        "stripe_refund_manual_action_required",
        "stripe_refund_failed",
    }


def test_refund_consistency_checks_confirmed_row_sum_and_application_marker():
    confirmed = SimpleNamespace(
        id=4,
        status="provider_confirmed",
        amount=Decimal("5.00"),
        provider_refund_id="re_ok",
        provider_status="succeeded",
        failure_reason=None,
        accounting_applied_at=NOW,
    )
    unapplied = SimpleNamespace(
        id=5,
        status="provider_confirmed",
        amount=Decimal("2.00"),
        provider_refund_id="re_bad",
        provider_status="succeeded",
        failure_reason=None,
        accounting_applied_at=None,
    )
    payment = make_payment(
        id=38,
        status="succeeded",
        amount=Decimal("19.00"),
        refunded_amount=Decimal("7.00"),
        refunds=[confirmed, unapplied],
    )

    issue_types = {issue.issue_type for issue in refund_consistency_issues("stripe", [payment])}
    assert issue_types == {
        "stripe_confirmed_refund_total_mismatch",
        "stripe_confirmed_refund_not_applied",
    }


# --- GK-426: renewals must not manufacture permanent false criticals ----------


class RenewalSession:
    """Just enough session for `create_or_extend_subscription` over existing rows."""

    def __init__(self, *existing):
        self.existing = list(existing)
        self.added = []

    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.existing)))

    def add(self, obj):
        self.added.append(obj)
        self.existing.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 99


@pytest.fixture
def no_access_floor(monkeypatch):
    """GK-439's launch floor is orthogonal here; pin it off so dates stay predictable."""
    monkeypatch.setattr(
        "app.services.subscription.settings",
        SimpleNamespace(access_start_floor_at=None),
    )


@pytest.mark.asyncio
async def test_renewal_after_lapse_releases_provider_link_from_the_old_row(no_access_floor):
    lapsed = make_subscription(
        id=2,
        status="expired",
        expires_at=NOW - timedelta(days=1),
        current_period_end=NOW - timedelta(days=1),
    )
    session = RenewalSession(lapsed)

    renewed = await create_or_extend_subscription(
        session,
        user=SimpleNamespace(id=3, tg_id=1003),
        plan=SimpleNamespace(id=2, code="1m", name="1 month", duration_days=30),
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_live_1",
        provider_status="active",
    )

    assert renewed is not lapsed
    assert renewed.provider_subscription_id == "sub_live_1"
    assert lapsed.provider_subscription_id is None
    assert lapsed.provider_status is None
    # The period dates stay put, so access arithmetic on history is unchanged.
    assert lapsed.current_period_end == NOW - timedelta(days=1)


@pytest.mark.asyncio
async def test_simulated_renewal_produces_no_reconciliation_item(no_access_floor):
    lapsed = make_subscription(
        id=2,
        status="expired",
        expires_at=NOW - timedelta(days=1),
        current_period_end=NOW - timedelta(days=1),
    )
    session = RenewalSession(lapsed)

    renewed = await create_or_extend_subscription(
        session,
        user=SimpleNamespace(id=3, tg_id=1003),
        plan=SimpleNamespace(id=2, code="1m", name="1 month", duration_days=30),
        source="stripe",
        provider="stripe",
        provider_subscription_id="sub_live_1",
        provider_status="active",
    )

    assert scan_stripe_local([], [lapsed, renewed], [], NOW) == []


def test_stale_provider_link_on_a_superseded_row_is_not_judged():
    """The belt-and-braces filter: a row that predates the backfill is still ignored."""
    stale = make_subscription(
        id=2,
        status="expired",
        provider_status="active",
        expires_at=NOW - timedelta(days=1),
        current_period_end=NOW - timedelta(days=1),
    )
    current = make_subscription(id=8)

    assert current_provider_subscription_rows([stale, current]) == [current]
    assert scan_stripe_local([], [stale, current], [], NOW) == []
    assert scan_lava_local([], [_as_lava(stale), _as_lava(current)], [], NOW) == []


def _as_lava(sub):
    clone = SimpleNamespace(**vars(sub))
    clone.provider = "lava"
    clone.source = "lava"
    if clone.provider_subscription_id:
        clone.provider_subscription_id = "lava_contract_1"
    return clone


def test_rows_without_a_provider_link_are_never_filtered_out():
    a = make_subscription(id=2, provider_subscription_id=None)
    b = make_subscription(id=3, provider_subscription_id=None)
    assert current_provider_subscription_rows([a, b]) == [a, b]
    assert "stripe_active_subscription_missing_provider_id" in {
        issue.issue_type for issue in scan_stripe_local([], [a, b], [], NOW)
    }


def _released_superseded_row(**overrides):
    """The row shape GK-426 actually leaves behind on the live host.

    `release_superseded_provider_link` nulls the provider link, and nothing flips
    `status` — that only happens when a Telegram revoke succeeds, which for a
    member who renewed may never have run. So the row sits at `status="active"`
    with lapsed dates and no link. GK-426's own tests wrote `status="expired"`
    here, which is why the class this shape generates was invisible until the
    19.08 audit read it off subscription 2.
    """
    data = {
        "id": 2,
        "provider_subscription_id": None,
        "provider_status": None,
        "status": "active",
        "expires_at": NOW - timedelta(days=1),
        "current_period_end": NOW - timedelta(days=1),
    }
    data.update(overrides)
    return make_subscription(**data)


def test_a_released_superseded_row_is_not_flagged_for_the_link_gk426_removed():
    """GK-469: the fix must not manufacture the critical it was meant to remove."""
    released = _released_superseded_row()

    assert current_provider_subscription_rows([released]) == [released]
    assert scan_stripe_local([], [released], [], NOW) == []
    assert scan_lava_local([], [_as_lava(released)], [], NOW) == []


def test_a_renewal_pair_in_the_live_shape_reports_nothing_at_all():
    """Subscription 2 released, subscription 8 live — the exact live pair."""
    released = _released_superseded_row(id=2)
    live = make_subscription(id=8, provider_subscription_id="sub_live_1")

    assert scan_stripe_local([], [released, live], [], NOW) == []


def test_a_live_row_missing_its_provider_link_is_still_critical():
    """The genuine case the rule exists for survives the guard."""
    live_unlinked = make_subscription(id=3, provider_subscription_id=None)

    assert "stripe_active_subscription_missing_provider_id" in {
        issue.issue_type for issue in scan_stripe_local([], [live_unlinked], [], NOW)
    }
    assert "lava_active_subscription_missing_provider_id" in {
        issue.issue_type for issue in scan_lava_local([], [_as_lava(live_unlinked)], [], NOW)
    }


def test_a_comp_row_is_never_reported_for_a_provider_link_it_never_had():
    """GK-483 × GK-469: the defect neither branch could see on its own.

    GK-483 gives the client's own team a flag that ends the expiry clock, which
    it implements by making `has_subscription_access` return True for a comp row
    whatever its dates say. The guard added above reads exactly that predicate.
    Put the two together and every flagged team member becomes, to this rule, an
    active subscription with no provider id — a critical, re-filed nightly,
    which is the class GK-469 exists to remove.

    A comp row is team access. Nobody paid for it, no provider ever issued a
    contract, so a missing provider link is its definition and not a finding.

    This case is live **by date**, so it fails without `_is_comp_row` on this
    branch alone — the GK-483 shortcut is not needed to prove the rule.
    """
    comped = make_subscription(id=9, provider_subscription_id=None, is_comp=True)

    assert scan_stripe_local([], [comped], [], NOW) == []
    assert scan_lava_local([], [_as_lava(comped)], [], NOW) == []


def test_the_comp_exclusion_is_the_flag_and_not_the_dates():
    """The same row unflagged still reports — the guard narrowed one rule.

    Written as a pair with the test above because "returns no issues" is also
    what a rule that stopped working looks like.
    """
    unflagged = make_subscription(id=9, provider_subscription_id=None)

    assert "stripe_active_subscription_missing_provider_id" in {
        issue.issue_type for issue in scan_stripe_local([], [unflagged], [], NOW)
    }


def test_grants_released_row_stays_quiet_once_it_is_flagged_as_team():
    """`sub#2` — the exact row GK-484 names and GK-483's roster resolves by comp.

    Today it is quiet because it entitles nobody. After GK-483 merges ahead of
    this branch it is live-by-flag, and `_is_comp_row` is the only thing keeping
    it quiet. Asserting both shapes here pins the row through that transition
    rather than after somebody notices the criticals.
    """
    released_and_comped = _released_superseded_row(is_comp=True)

    assert scan_stripe_local([], [released_and_comped], [], NOW) == []
    assert scan_lava_local([], [_as_lava(released_and_comped)], [], NOW) == []


def test_a_banned_members_missing_link_is_not_reported():
    """Access revoked by a ban is still no access, so there is nothing to compare.

    The row is deliberately left with `provider_status="active"`, so it still
    reports `stripe_provider_active_without_local_access` — a real finding, and
    the check that this guard narrowed one rule rather than silencing the row.
    """
    banned = make_subscription(
        id=4, provider_subscription_id=None, access_revoked_at=NOW - timedelta(hours=1)
    )

    issue_types = {issue.issue_type for issue in scan_stripe_local([], [banned], [], NOW)}
    assert "stripe_active_subscription_missing_provider_id" not in issue_types
    assert issue_types == {"stripe_provider_active_without_local_access"}


def test_current_row_discrepancies_still_fire_after_the_filter():
    provider_active_no_access = make_subscription(
        id=4,
        provider_subscription_id="sub_a",
        provider_status="active",
        expires_at=NOW - timedelta(days=2),
        current_period_end=NOW - timedelta(days=2),
    )
    provider_terminal_with_access = make_subscription(
        id=5,
        provider_subscription_id="sub_b",
        provider_status="canceled",
    )

    issue_types = {
        issue.issue_type
        for issue in scan_stripe_local(
            [], [provider_active_no_access, provider_terminal_with_access], [], NOW
        )
    }
    assert issue_types == {
        "stripe_provider_active_without_local_access",
        "stripe_provider_terminal_with_local_access",
    }


def make_remote_subscription(**overrides):
    """`compare_stripe_remote` reads the wall clock, so anchor access to it."""
    live = datetime.now(UTC)
    data = {
        "started_at": live - timedelta(days=10),
        "expires_at": live + timedelta(days=20),
        "current_period_start": live - timedelta(days=10),
        "current_period_end": live + timedelta(days=20),
    }
    data.update(overrides)
    return make_subscription(**data)


class StubStripeGateway:
    def __init__(self, subscriptions):
        self.subscriptions = subscriptions
        self.subscription_calls = []

    async def retrieve_invoice(self, invoice_id):
        return None

    async def retrieve_subscription(self, subscription_id):
        self.subscription_calls.append(subscription_id)
        return self.subscriptions.get(subscription_id)


@pytest.mark.asyncio
async def test_remote_terminal_state_is_not_critical_when_cancelling_at_period_end():
    """A normal cancellation is terminal remotely and legitimately open locally."""
    cancelling = make_remote_subscription(
        id=6, provider_subscription_id="sub_c", cancel_at_period_end=True
    )
    gateway = StubStripeGateway({"sub_c": {"id": "sub_c", "status": "canceled"}})

    issues = await compare_stripe_remote([], [cancelling], gateway=gateway)

    assert issues == []


@pytest.mark.asyncio
async def test_remote_terminal_state_is_still_critical_without_a_cancellation():
    revoked_too_late = make_remote_subscription(id=7, provider_subscription_id="sub_d")
    gateway = StubStripeGateway({"sub_d": {"id": "sub_d", "status": "canceled"}})

    issues = await compare_stripe_remote([], [revoked_too_late], gateway=gateway)

    assert [issue.issue_type for issue in issues] == [
        "stripe_remote_terminal_subscription_with_local_access"
    ]


@pytest.mark.asyncio
async def test_remote_comparison_skips_superseded_rows_entirely():
    """Not just quieter — one fewer Stripe API call per stale row, per night."""
    stale = make_remote_subscription(
        id=2, status="expired", expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    current = make_remote_subscription(id=8)
    gateway = StubStripeGateway({"sub_live_1": {"id": "sub_live_1", "status": "active"}})

    await compare_stripe_remote([], [stale, current], gateway=gateway)

    assert gateway.subscription_calls == ["sub_live_1"]


# --- GK-430: a resolution has to survive the next run, and the next sixty -----


def _stripe_issue(**overrides):
    data = {
        "provider": "stripe",
        "severity": "critical",
        "issue_type": "stripe_paid_invoice_not_succeeded",
        "entity_type": "payment",
        "entity_id": 123,
        "external_id": "in_123",
        "title": "Stripe paid invoice mismatch",
        "description": "Mocked discrepancy",
        "expected_state": {"payment.status": "succeeded"},
        "observed_state": {"payment.status": "pending"},
    }
    data.update(overrides)
    return ReconciliationIssue(**data)


async def _run_with(session, *issues):
    async def collector(_session, _now):
        return list(issues)

    return await run_reconciliation(
        session,
        providers=["stripe"],
        triggered_by="test",
        now=NOW,
        collectors={"stripe": collector},
    )


def _items(session):
    return [obj for obj in session.added if isinstance(obj, ReconciliationItem)]


@pytest.mark.asyncio
async def test_a_resolved_condition_comes_back_resolved_not_reopened():
    resolved_yesterday = make_prior_item(
        status="resolved",
        resolve_note="historical test data, checked against Stripe",
        resolved_by_admin_id=3,
        resolved_at=NOW - timedelta(days=1),
    )
    session = FakeSession(prior_items=[resolved_yesterday])

    run = await _run_with(session, _stripe_issue())

    item = _items(session)[0]
    assert item.status == "resolved"
    assert item.resolve_note == "historical test data, checked against Stripe"
    assert item.resolved_by_admin_id == 3
    assert item.resolved_at == NOW - timedelta(days=1)
    assert run.open_items_count == 0
    assert run.items_count == 1


@pytest.mark.asyncio
async def test_resolution_carries_across_two_further_runs():
    """The acceptance criterion, run out to the third night."""
    prior = make_prior_item(
        status="resolved",
        resolve_note="explained",
        resolved_by_admin_id=3,
        resolved_at=NOW - timedelta(days=1),
    )
    for _ in range(2):
        session = FakeSession(prior_items=[prior])
        run = await _run_with(session, _stripe_issue())
        prior = _items(session)[0]
        prior.id = 900
        assert run.open_items_count == 0

    assert prior.status == "resolved"
    assert prior.resolve_note == "explained"


@pytest.mark.asyncio
async def test_a_reopened_condition_is_open_again_on_the_next_run():
    session = FakeSession(prior_items=[make_prior_item(status="open")])

    run = await _run_with(session, _stripe_issue())

    assert _items(session)[0].status == "open"
    assert run.open_items_count == 1


@pytest.mark.asyncio
async def test_a_deliberately_injected_discrepancy_still_appears_and_alerts():
    """A different entity is a different condition — someone else's resolution does not cover it."""
    resolved = make_prior_item(
        entity_id="123", status="resolved", resolve_note="explained", resolved_by_admin_id=3
    )
    session = FakeSession(prior_items=[resolved])

    run = await _run_with(session, _stripe_issue(entity_id=999, external_id="in_999"))

    assert _items(session)[0].status == "open"
    assert run.open_items_count == 1
    assert run.summary["new_items"] == 1
    assert "1 NEW discrepancy item(s)" in reconciliation_summary_text(run)


@pytest.mark.asyncio
async def test_first_seen_is_carried_so_a_standing_item_does_not_look_new():
    session = FakeSession(prior_items=[make_prior_item(status="open")])

    run = await _run_with(session, _stripe_issue())

    assert _items(session)[0].first_seen_at == NOW - timedelta(days=60)
    assert run.summary["new_items"] == 0
    assert run.summary["recurring_items"] == 1
    assert "nothing new, 1 open item(s)" in reconciliation_summary_text(run)


@pytest.mark.asyncio
async def test_a_first_sighting_stamps_first_seen_with_this_run():
    session = FakeSession()

    run = await _run_with(session, _stripe_issue())

    assert _items(session)[0].first_seen_at == NOW
    assert run.summary["new_items"] == 1


@pytest.mark.asyncio
async def test_the_same_condition_twice_in_one_run_is_one_row():
    session = FakeSession()

    run = await _run_with(session, _stripe_issue(), _stripe_issue(title="same thing, said twice"))

    assert len(_items(session)) == 1
    assert run.items_count == 1
    assert run.summary["duplicates_dropped"] == 1


@pytest.mark.asyncio
async def test_a_clean_day_reports_no_open_items():
    session = FakeSession()

    run = await _run_with(session)

    assert run.open_items_count == 0
    assert "no open discrepancy items" in reconciliation_summary_text(run)


# --- GK-415: what Lava thinks it sold, compared against what we recorded ------
#
# The one direction the local scans structurally cannot see. Every check below
# starts from a *remote* sale, because the failure being hunted — 16.07, and the
# reason Grant asked for this — is a paid buyer with no local row at all.


LAVA_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def make_lava_payment(**overrides):
    data = {
        "id": 94,
        "provider": "lava",
        "status": "succeeded",
        "amount": Decimal("1500.00"),
        "currency": "RUB",
        "lava_invoice_id": "inv-abc",
        "external_id": "inv-abc",
        "created_at": LAVA_NOW - timedelta(days=2),
    }
    data.update(overrides)
    return make_payment(**data)


def make_lava_subscription(**overrides):
    data = {"id": 40, "user_id": 3, "source": "lava", "provider": "lava"}
    data.update(overrides)
    return make_subscription(**data)


def remote_sale(**overrides):
    """The shape observed on real money (GK-418, the 29.07 invoice)."""
    raw = {
        "id": "inv-abc",
        "status": "completed",
        "receipt": {"amount": 1500, "currency": "RUB"},
        "createdAt": (LAVA_NOW - timedelta(days=2)).isoformat(),
        "clientUtm": {"utm_content": "payment_94"},
        "buyer": {"email": "Buyer@example.com"},
    }
    raw.update(overrides)
    return RemoteSale(raw)


class StubLavaGateway:
    def __init__(self, sales=(), error=None):
        self.sales = list(sales)
        self.error = error
        self.calls = []

    async def list_recent_sales(self, *, since):
        self.calls.append(since)
        if self.error is not None:
            raise self.error
        return self.sales


def test_remote_sale_parses_the_shape_seen_on_real_money():
    sale = remote_sale()

    assert sale.is_completed
    assert sale.payment_id == 94
    assert sale.amount == Decimal("1500.00")
    assert sale.currency == "RUB"
    assert sale.buyer_email == "buyer@example.com"
    assert sale.created_at == LAVA_NOW - timedelta(days=2)


@pytest.mark.parametrize("utm", [None, "", "payment_", "payment_abc", "campaign_94", "94"])
def test_a_utm_we_did_not_write_yields_no_payment_id(utm):
    assert RemoteSale({"clientUtm": {"utm_content": utm}}).payment_id is None


@pytest.mark.asyncio
async def test_a_completed_remote_sale_with_no_local_payment_is_critical():
    """The 16.07 failure: money reached Lava, nothing reached us."""
    gateway = StubLavaGateway([remote_sale()])

    issues = await compare_lava_remote([], [], gateway=gateway, now=LAVA_NOW)

    assert [i.issue_type for i in issues] == ["lava_remote_sale_without_local_payment"]
    assert issues[0].severity == "critical"
    # The alert has to carry enough to fulfil by hand — nothing else knows it exists.
    assert issues[0].observed_state["remote_sale"]["buyer_email"] == "buyer@example.com"
    assert issues[0].observed_state["remote_sale"]["amount"] == "1500.00"


@pytest.mark.asyncio
async def test_a_completed_sale_whose_local_payment_never_succeeded_is_critical():
    payment = make_lava_payment(status="pending")
    gateway = StubLavaGateway([remote_sale()])

    issues = await compare_lava_remote([payment], [], gateway=gateway, now=LAVA_NOW)

    assert [i.issue_type for i in issues] == ["lava_remote_completed_local_not_succeeded"]


@pytest.mark.asyncio
async def test_a_price_disagreement_is_never_a_silent_pass():
    payment = make_lava_payment(amount=Decimal("1200.00"))
    gateway = StubLavaGateway([remote_sale()])

    issues = await compare_lava_remote(
        [payment], [make_lava_subscription()], gateway=gateway, now=LAVA_NOW
    )

    assert [i.issue_type for i in issues] == ["lava_remote_amount_mismatch"]
    assert issues[0].expected_state == {"amount": "1200.00", "currency": "RUB"}
    assert issues[0].observed_state["remote_amount"] == "1500.00"


@pytest.mark.asyncio
async def test_a_currency_disagreement_is_reported_too():
    payment = make_lava_payment(currency="USD")
    gateway = StubLavaGateway([remote_sale()])

    issues = await compare_lava_remote(
        [payment], [make_lava_subscription()], gateway=gateway, now=LAVA_NOW
    )

    assert [i.issue_type for i in issues] == ["lava_remote_amount_mismatch"]


@pytest.mark.asyncio
async def test_an_unreadable_remote_amount_is_not_reported_as_a_mismatch():
    """Unreadable is not wrong. Otherwise a shape change becomes a night of criticals."""
    sale = remote_sale(receipt={"currency": "RUB"}, amount=None)
    gateway = StubLavaGateway([sale])

    issues = await compare_lava_remote(
        [make_lava_payment()], [make_lava_subscription()], gateway=gateway, now=LAVA_NOW
    )

    assert sale.amount is None
    assert issues == []


@pytest.mark.asyncio
async def test_a_paid_sale_that_produced_no_subscription_is_critical():
    gateway = StubLavaGateway([remote_sale()])

    issues = await compare_lava_remote([make_lava_payment()], [], gateway=gateway, now=LAVA_NOW)

    assert [i.issue_type for i in issues] == ["lava_remote_sale_never_fulfilled"]


@pytest.mark.asyncio
async def test_a_fully_accounted_sale_reports_nothing():
    gateway = StubLavaGateway([remote_sale()])

    issues = await compare_lava_remote(
        [make_lava_payment()], [make_lava_subscription()], gateway=gateway, now=LAVA_NOW
    )

    assert issues == []


@pytest.mark.asyncio
async def test_incomplete_remote_sales_are_ignored():
    gateway = StubLavaGateway([remote_sale(status="in-progress"), remote_sale(status="failed")])

    issues = await compare_lava_remote([], [], gateway=gateway, now=LAVA_NOW)

    assert issues == []


@pytest.mark.asyncio
async def test_a_utm_naming_a_payment_we_do_not_have_does_not_guess_by_email():
    """Attaching it to a stranger's payment would silence the finding entirely."""
    someone_else = make_lava_payment(
        id=95, note="buyer_email=buyer@example.com", lava_invoice_id=None, external_id=None
    )
    gateway = StubLavaGateway([remote_sale()])

    issues = await compare_lava_remote(
        [someone_else], [make_lava_subscription()], gateway=gateway, now=LAVA_NOW
    )

    assert [i.issue_type for i in issues] == ["lava_remote_sale_without_local_payment"]


@pytest.mark.asyncio
async def test_a_sale_without_utm_falls_back_to_email_amount_and_time():
    payment = make_lava_payment(
        note="referral_discount=ABC | buyer_email=buyer@example.com",
        lava_invoice_id=None,
        external_id=None,
        created_at=LAVA_NOW - timedelta(days=2, hours=1),
    )
    gateway = StubLavaGateway([remote_sale(clientUtm={})])

    issues = await compare_lava_remote(
        [payment], [make_lava_subscription()], gateway=gateway, now=LAVA_NOW
    )

    assert issues == []


@pytest.mark.asyncio
async def test_the_email_fallback_refuses_a_match_a_day_apart():
    payment = make_lava_payment(
        note="buyer_email=buyer@example.com",
        lava_invoice_id=None,
        external_id=None,
        created_at=LAVA_NOW - timedelta(days=3),
    )
    gateway = StubLavaGateway([remote_sale(clientUtm={})])

    issues = await compare_lava_remote(
        [payment], [make_lava_subscription()], gateway=gateway, now=LAVA_NOW
    )

    assert [i.issue_type for i in issues] == ["lava_remote_sale_without_local_payment"]


@pytest.mark.asyncio
async def test_sales_older_than_the_window_age_out():
    """GK-426's lesson: an unmatchable finding must be able to stop being reported."""
    old = remote_sale(
        createdAt=(LAVA_NOW - timedelta(days=LAVA_REMOTE_LOOKBACK_DAYS + 1)).isoformat()
    )
    gateway = StubLavaGateway([old])

    issues = await compare_lava_remote([], [], gateway=gateway, now=LAVA_NOW)

    assert issues == []
    assert gateway.calls == [LAVA_NOW - timedelta(days=LAVA_REMOTE_LOOKBACK_DAYS)]


@pytest.mark.asyncio
async def test_a_dead_lava_journal_reports_itself_instead_of_killing_the_run():
    gateway = StubLavaGateway(error=RuntimeError("gate.lava.top timed out"))

    issues = await compare_lava_remote([], [], gateway=gateway, now=LAVA_NOW)

    assert [i.issue_type for i in issues] == ["lava_invoice_journal_fetch_failed"]


@pytest.mark.asyncio
async def test_no_lava_key_means_no_remote_call_at_all(monkeypatch):
    """Without credentials the nightly run is exactly what it was before GK-415."""
    gateway = StubLavaGateway([remote_sale()])
    monkeypatch.setattr(reconciliation, "settings", SimpleNamespace(lava_api_key=""))

    issues = await reconciliation.collect_lava_discrepancies(FakeSession(), NOW, gateway=gateway)

    assert gateway.calls == []
    assert issues == []


@pytest.mark.asyncio
async def test_a_lava_key_turns_the_remote_compare_on(monkeypatch):
    gateway = StubLavaGateway([remote_sale()])
    monkeypatch.setattr(reconciliation, "settings", SimpleNamespace(lava_api_key="live-key"))

    issues = await reconciliation.collect_lava_discrepancies(
        FakeSession(), LAVA_NOW, gateway=gateway
    )

    assert len(gateway.calls) == 1
    assert [i.issue_type for i in issues] == ["lava_remote_sale_without_local_payment"]
