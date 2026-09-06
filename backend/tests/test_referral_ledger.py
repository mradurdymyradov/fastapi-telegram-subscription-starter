from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from dateutil.relativedelta import relativedelta

from app.config import Settings, parse_fx_rates_to_usd
from app.db.models import Referral, ReferralCommission, ReferralPayoutBatch
from app.services import referral_ledger
from app.services.referral_ledger import (
    PayoutTransitionError,
    adjust_commission_for_refund,
    cancel_pending_commission_for_payment,
    cancel_pending_commission_for_referee,
    create_referral_payout_batch,
    record_referral_commission_intent,
    transition_referral_payout_batch,
    vest_due_commissions,
    vesting_at_for_payment,
)

NOW = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.value or [])


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.queries = []
        self.flushes = 0
        self._next_id = 500

    async def execute(self, query):
        self.queries.append(query)
        return Result(self.results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


def user(**overrides):
    data = {"id": 10, "tg_id": 10010, "referrer_id": 99, "bonus_days": 0}
    data.update(overrides)
    return SimpleNamespace(**data)


def payment(**overrides):
    data = {
        "id": 20,
        "user_id": 10,
        "provider": "stripe",
        "amount": Decimal("19.00"),
        "currency": "USD",
        "external_id": "sub_123",
        "stripe_invoice_id": "in_123",
        "lava_invoice_id": None,
        "provider_event_id": "evt_123",
        "tx_hash": None,
        "is_gift": False,
        "is_renewal": False,
        "billing_period_start": NOW,
        "billing_period_end": NOW + relativedelta(months=1),
        "created_at": NOW,
        "approved_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def referral(**overrides):
    data = {
        "id": 30,
        "referrer_id": 99,
        "referee_id": 10,
        "first_payment_id": 20,
        "partner_earning_started_at": NOW,
        "partner_earning_ends_at": NOW + relativedelta(months=12),
        "retention_streak_started_at": NOW,
        "retention_coverage_ends_at": NOW + relativedelta(months=1),
        "retention_gate_at": NOW + relativedelta(months=3),
        "retention_qualified_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_first_paid_referral_creates_pending_commission_without_legacy_bonus_days():
    referrer = user(id=99, bonus_days=14)
    referee = user(id=10, referrer_id=99)
    first_payment = payment()
    session = FakeSession(None, referrer, None, None)

    commission = await record_referral_commission_intent(session, referee, first_payment)

    referral = next(obj for obj in session.added if isinstance(obj, Referral))
    assert commission is next(obj for obj in session.added if isinstance(obj, ReferralCommission))
    assert referral.bonus_days_granted == 0
    assert referrer.bonus_days == 14
    assert commission.referral_id == referral.id
    assert commission.referrer_id == 99
    assert commission.referee_id == 10
    assert commission.source_payment_id == first_payment.id
    assert commission.source_invoice_id == "in_123"
    assert commission.source_provider_event_id == "evt_123"
    assert commission.status == "pending"
    assert commission.vests_at == vesting_at_for_payment(first_payment)
    assert commission.vests_at.month == 8
    assert commission.vests_at.day == 31
    assert commission.amount_usd == Decimal("3.80")
    assert referral.partner_earning_started_at == NOW
    assert referral.partner_earning_ends_at == NOW + relativedelta(months=12)
    assert referral.retention_gate_at == NOW + relativedelta(months=3)
    assert referral.retention_coverage_ends_at == NOW + relativedelta(months=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paid", "expected_filter"),
    [
        (
            payment(
                id=21,
                stripe_invoice_id=None,
                provider_event_id=None,
                external_id=None,
            ),
            "source_payment_id",
        ),
        (
            payment(
                id=None,
                stripe_invoice_id="in_duplicate",
                provider_event_id=None,
                external_id=None,
            ),
            "source_invoice_id",
        ),
        (
            payment(
                id=None,
                stripe_invoice_id=None,
                provider_event_id="evt_duplicate",
                external_id=None,
            ),
            "source_provider_event_id",
        ),
    ],
    ids=["source-payment", "provider-invoice", "provider-event"],
)
async def test_payment_idempotency_returns_existing_commission_without_new_rows(
    paid,
    expected_filter,
):
    existing = SimpleNamespace(id=77, status="pending")
    session = FakeSession(existing)

    commission = await record_referral_commission_intent(session, user(referrer_id=99), paid)

    assert commission is existing
    assert session.added == []
    assert session.flushes == 0
    # The fake session supplies the matching row; inspect the generated query so
    # each independent replay key remains part of the idempotency contract.
    assert expected_filter in str(session.queries[0])


# --- GK-457: a rouble is not a dollar --------------------------------------
#
# The whole ledger -- the $100 threshold, the payout batches, Grant's approved
# "from $100 accrued" partner copy -- is denominated in dollars, and RUB is the
# audience's main payment method. These cover the seam.

RUB_RATE = Decimal("0.0125")


def rub_payment(**overrides):
    data = {
        "amount": Decimal("1500.00"),
        "currency": "RUB",
        "provider": "lava",
        "stripe_invoice_id": None,
        "lava_invoice_id": "inv_rub_1",
    }
    data.update(overrides)
    return payment(**data)


@pytest.fixture
def rouble_rate(monkeypatch):
    """Pin the RUB rate so the assertions below don't move with the setting."""
    monkeypatch.setattr(
        referral_ledger, "_configured_fx_rates", lambda: {"RUB": RUB_RATE}
    )
    return RUB_RATE


@pytest.fixture
def no_rates(monkeypatch):
    monkeypatch.setattr(referral_ledger, "_configured_fx_rates", dict)


@pytest.fixture
def sent_alerts(monkeypatch):
    sent: list[tuple[str, dict]] = []

    async def _capture(text, **kwargs):
        sent.append((text, kwargs))
        return True

    monkeypatch.setattr(referral_ledger, "send_ops_alert", _capture)
    return sent


def test_the_shipped_default_rate_prices_the_rouble_tariff_near_its_dollar_one():
    """1500 RUB and $19 are the same tariff, so they should be worth about the
    same commission. The default is read off our own price list, not a market
    feed -- if the rouble prices move and this drifts, that is the signal to
    revisit the setting."""
    rates = parse_fx_rates_to_usd(
        Settings.model_fields["referral_fx_rates_to_usd"].default
    )
    assert rates["RUB"] * Decimal("1500") == Decimal("18.7500")


@pytest.mark.parametrize(
    "raw",
    [
        "RUB",  # no colon at all
        "RUB:",  # the shape a truncated copy-paste leaves
        "RUB:0",  # a zero rate would price every rouble commission at $0
        "RUB:-0.0125",
        "R U B:0.0125",
        "USD:1",  # the ledger's own currency is never repriced from a table
    ],
)
def test_a_malformed_rate_table_is_refused_rather_than_read_as_empty(raw):
    """Reading it as empty would be silent: every rouble commission would accrue
    $0 and only the ops chat would ever say so. `validate_security` is the gate."""
    with pytest.raises(ValueError):
        parse_fx_rates_to_usd(raw)


@pytest.mark.asyncio
async def test_a_rouble_payment_accrues_dollars_and_records_the_rate(rouble_rate):
    """The defect: 1500 RUB wrote 300 into a column summed and paid out as USD."""
    session = FakeSession(None, user(id=99), None)

    commission = await record_referral_commission_intent(
        session, user(id=10, referrer_id=99), rub_payment()
    )

    # 1500 * 0.0125 = $18.75, of which the partner earns 20%.
    assert commission.amount_usd == Decimal("3.75")
    assert commission.fx_rate_to_usd == rouble_rate
    # The basis is kept in the currency it was actually paid in.
    assert commission.source_amount == Decimal("1500.00")
    assert commission.source_currency == "RUB"


@pytest.mark.asyncio
async def test_a_dollar_payment_keeps_its_amount_and_a_rate_of_one(rouble_rate):
    session = FakeSession(None, user(id=99), None)

    commission = await record_referral_commission_intent(
        session, user(id=10, referrer_id=99), payment()
    )

    assert commission.amount_usd == Decimal("3.80")
    assert commission.fx_rate_to_usd == Decimal("1")


@pytest.mark.asyncio
async def test_an_unpriced_currency_is_held_at_zero_and_alerted_not_guessed(
    no_rates,
    sent_alerts,
):
    """$0 with a null rate is recoverable from `source_amount` by hand. A guessed
    number that looks payable is not -- it goes out in a batch and is gone."""
    session = FakeSession(None, user(id=99), None)

    commission = await record_referral_commission_intent(
        session, user(id=10, referrer_id=99), rub_payment()
    )

    assert commission.amount_usd == Decimal("0.00")
    assert commission.fx_rate_to_usd is None
    assert commission.source_amount == Decimal("1500.00")
    assert len(sent_alerts) == 1
    text, kwargs = sent_alerts[0]
    assert "RUB" in text and "REFERRAL_FX_RATES_TO_USD" in text
    assert kwargs["key"] == "referral_fx_missing:RUB"


@pytest.mark.asyncio
async def test_a_currency_code_from_a_webhook_cannot_break_the_alert(
    no_rates,
    sent_alerts,
):
    """`payment.currency` comes off a provider payload. GK-451 is the open task
    for an ops alert that Telegram rejected because markup reached it."""
    session = FakeSession(None, user(id=99), None)

    commission = await record_referral_commission_intent(
        session, user(id=10, referrer_id=99), rub_payment(currency="<b>RUB</b>")
    )

    assert commission.source_currency == "BRUBB"
    assert "<b>" not in sent_alerts[0][0]


@pytest.mark.asyncio
async def test_a_full_rouble_refund_claws_back_the_dollars_that_were_accrued(
    rouble_rate,
):
    gate = NOW + relativedelta(months=3)
    source = SimpleNamespace(
        id=95,
        referral_id=30,
        status="vested",
        amount_usd=Decimal("3.75"),
        fx_rate_to_usd=RUB_RATE,
        vested_at=gate,
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    session = FakeSession(source)

    action, adjustment = await adjust_commission_for_refund(
        session,
        rub_payment(),
        refund_amount=Decimal("1500.00"),
        refunded_total=Decimal("1500.00"),
        payment_total=Decimal("1500.00"),
        fully_refunded=True,
        reason="rouble refund",
        now=gate + timedelta(seconds=1),
    )

    # Not -300.00, which is what 20% of an unconverted rouble basis would give.
    assert action == "adjusted"
    assert adjustment == Decimal("-3.75")


@pytest.mark.asyncio
async def test_a_partial_rouble_refund_reduces_a_pending_row_in_dollars(rouble_rate):
    source = SimpleNamespace(
        id=96,
        referral_id=30,
        status="pending",
        amount_usd=Decimal("3.75"),
        fx_rate_to_usd=RUB_RATE,
        vests_at=NOW + relativedelta(months=3),
        vested_at=None,
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    session = FakeSession(source)

    action, adjustment = await adjust_commission_for_refund(
        session,
        rub_payment(),
        refund_amount=Decimal("750.00"),
        refunded_total=Decimal("750.00"),
        payment_total=Decimal("1500.00"),
        fully_refunded=False,
        reason="half back",
        now=NOW + relativedelta(months=1),
    )

    assert action == "reduced"
    assert source.amount_usd == Decimal("1.88")  # 20% of 750 * 0.0125
    assert adjustment == Decimal("-1.87")


@pytest.mark.asyncio
async def test_a_row_held_at_zero_is_never_inflated_by_a_partial_refund(no_rates):
    """The clamp. Without it the unpriced row's refund path would compute a
    rouble figure and write it into `amount_usd` -- the original defect,
    arriving through the one door that was left open."""
    source = SimpleNamespace(
        id=97,
        referral_id=30,
        status="pending",
        amount_usd=Decimal("0.00"),
        fx_rate_to_usd=None,
        vests_at=NOW + relativedelta(months=3),
        vested_at=None,
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    source_referral = referral()
    session = FakeSession(source, source_referral, [source])

    action, adjustment = await adjust_commission_for_refund(
        session,
        rub_payment(),
        refund_amount=Decimal("750.00"),
        refunded_total=Decimal("750.00"),
        payment_total=Decimal("1500.00"),
        fully_refunded=False,
        reason="half back",
        now=NOW + relativedelta(months=1),
    )

    assert source.amount_usd == Decimal("0.00")
    assert adjustment == Decimal("0")
    assert action == "cancelled"


@pytest.mark.asyncio
async def test_a_pre_migration_row_is_refunded_in_the_units_it_was_written_in():
    """Rows accrued before GK-457 hold a rouble figure in `amount_usd`. They are
    not retroactively converted -- a migration rewriting money at a rate nobody
    reviewed is its own defect -- so a refund stays self-consistent with the row,
    and the set is left findable by `fx_rate_to_usd IS NULL`."""
    source = SimpleNamespace(
        id=98,
        referral_id=30,
        status="pending",
        amount_usd=Decimal("300.00"),  # the bug, as it sits in the table today
        fx_rate_to_usd=None,
        vests_at=NOW + relativedelta(months=3),
        vested_at=None,
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    session = FakeSession(source)

    action, adjustment = await adjust_commission_for_refund(
        session,
        rub_payment(),
        refund_amount=Decimal("750.00"),
        refunded_total=Decimal("750.00"),
        payment_total=Decimal("1500.00"),
        fully_refunded=False,
        reason="half back",
        now=NOW + relativedelta(months=1),
    )

    assert action == "reduced"
    assert source.amount_usd == Decimal("150.00")
    assert adjustment == Decimal("-150.00")


@pytest.mark.asyncio
async def test_refund_or_chargeback_before_vesting_cancels_pending_commission():
    commission = SimpleNamespace(
        id=77,
        referral_id=30,
        status="pending",
        vests_at=NOW + timedelta(days=30),
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    source_referral = referral(
        retention_coverage_ends_at=NOW + relativedelta(months=1),
        retention_gate_at=NOW + relativedelta(months=3),
    )
    session = FakeSession(commission, source_referral, [commission])

    cancelled = await cancel_pending_commission_for_payment(
        session,
        payment(status="refunded"),
        reason="stripe.refund",
        now=NOW,
    )

    assert cancelled is commission
    assert commission.status == "cancelled"
    assert commission.cancelled_at == NOW
    assert commission.cancellation_reason == "stripe.refund"
    assert source_referral.retention_streak_started_at is None
    assert source_referral.retention_coverage_ends_at is None


@pytest.mark.asyncio
async def test_due_pending_commissions_become_vested_for_payout_review():
    due = SimpleNamespace(
        id=77,
        referral_id=30,
        status="pending",
        vests_at=NOW - timedelta(seconds=1),
        vested_at=None,
        updated_at=None,
    )
    not_due = SimpleNamespace(
        id=78,
        referral_id=30,
        status="pending",
        vests_at=NOW + timedelta(days=1),
        vested_at=None,
        updated_at=None,
    )
    source_referral = referral(
        retention_gate_at=NOW - timedelta(seconds=1),
        retention_coverage_ends_at=NOW + timedelta(days=1),
    )
    session = FakeSession([due], [source_referral])

    vested = await vest_due_commissions(session, now=NOW)

    assert vested == [due]
    assert due.status == "vested"
    assert due.vested_at == NOW - timedelta(seconds=1)
    assert due.updated_at == NOW - timedelta(seconds=1)
    assert not_due.status == "pending"
    assert source_referral.retention_qualified_at == NOW - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_three_monthly_commissions_accumulate_then_vest_together_at_retention_gate():
    gate = NOW + relativedelta(months=3)
    pending = [
        SimpleNamespace(
            id=70 + month,
            referral_id=30,
            status="pending",
            vests_at=gate,
            vested_at=None,
            updated_at=None,
        )
        for month in range(1, 4)
    ]
    source_referral = referral(
        retention_streak_started_at=NOW,
        retention_coverage_ends_at=gate,
        retention_gate_at=gate,
    )

    vested = await vest_due_commissions(
        FakeSession(pending, [source_referral]),
        now=gate,
    )

    assert vested == pending
    assert [row.status for row in pending] == ["vested", "vested", "vested"]
    assert [row.vested_at for row in pending] == [gate, gate, gate]
    assert source_referral.retention_qualified_at == gate


@pytest.mark.asyncio
async def test_renewal_creates_an_additional_commission_for_same_referral():
    referrer = user(id=99)
    source_referral = referral()
    renewal = payment(
        id=21,
        stripe_invoice_id="in_renewal_1",
        provider_event_id="evt_renewal_1",
        is_renewal=True,
        billing_period_start=NOW + relativedelta(months=1),
        billing_period_end=NOW + relativedelta(months=2),
    )
    session = FakeSession(None, referrer, source_referral)

    commission = await record_referral_commission_intent(
        session,
        user(referrer_id=99),
        renewal,
        coverage_start=renewal.billing_period_start,
        coverage_end=renewal.billing_period_end,
    )

    assert isinstance(commission, ReferralCommission)
    assert commission.referral_id == source_referral.id
    assert commission.source_payment_id == 21
    assert commission.source_invoice_id == "in_renewal_1"
    assert commission.status == "pending"
    assert source_referral.retention_coverage_ends_at == NOW + relativedelta(months=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "paid_months", "expected_commission"),
    [
        (Decimal("89.00"), 6, Decimal("17.80")),
        (Decimal("149.00"), 12, Decimal("29.80")),
    ],
)
async def test_long_term_full_price_payment_waits_for_gate_then_vests(
    amount,
    paid_months,
    expected_commission,
):
    referrer = user(id=99)
    referee = user(id=10, referrer_id=99)
    paid = payment(
        amount=amount,
        billing_period_end=NOW + relativedelta(months=paid_months),
    )
    session = FakeSession(None, referrer, None)

    commission = await record_referral_commission_intent(
        session,
        referee,
        paid,
        coverage_start=NOW,
        coverage_end=paid.billing_period_end,
    )

    source_referral = next(obj for obj in session.added if isinstance(obj, Referral))
    assert commission.amount_usd == expected_commission
    assert commission.status == "pending"

    gate = NOW + relativedelta(months=3)
    vested = await vest_due_commissions(
        FakeSession([commission], [source_referral]),
        now=gate,
    )

    assert vested == [commission]
    assert commission.status == "vested"
    assert commission.vested_at == gate
    assert source_referral.retention_qualified_at == gate


@pytest.mark.asyncio
async def test_payment_after_retention_qualification_vests_without_fresh_hold():
    qualified_at = NOW + relativedelta(months=3)
    source_referral = referral(
        retention_qualified_at=qualified_at,
        retention_coverage_ends_at=NOW + relativedelta(months=6),
    )
    later_start = NOW + relativedelta(months=4)
    later = payment(
        id=24,
        amount=Decimal("19.00"),
        stripe_invoice_id="in_later",
        provider_event_id="evt_later",
        is_renewal=True,
        billing_period_start=later_start,
        billing_period_end=later_start + relativedelta(months=1),
    )
    session = FakeSession(None, user(id=99), source_referral)

    commission = await record_referral_commission_intent(
        session,
        user(referrer_id=99),
        later,
        coverage_start=later.billing_period_start,
        coverage_end=later.billing_period_end,
    )

    assert commission.status == "vested"
    assert commission.vests_at == later_start
    assert commission.vested_at == later_start


@pytest.mark.asyncio
async def test_payment_at_original_twelve_month_boundary_is_not_commissioned():
    window_end = NOW + relativedelta(months=12)
    source_referral = referral(partner_earning_ends_at=window_end)
    outside = payment(
        id=25,
        stripe_invoice_id="in_outside",
        provider_event_id="evt_outside",
        is_renewal=True,
        billing_period_start=window_end,
        billing_period_end=window_end + relativedelta(months=1),
    )
    session = FakeSession(None, user(id=99), source_referral)

    commission = await record_referral_commission_intent(
        session,
        user(referrer_id=99),
        outside,
        coverage_start=outside.billing_period_start,
        coverage_end=outside.billing_period_end,
    )

    assert commission is None
    assert session.added == []


@pytest.mark.asyncio
async def test_subscription_cancellation_before_gate_cancels_all_pending_rows():
    source_referral = referral()
    pending = [
        SimpleNamespace(
            id=71,
            referral_id=30,
            status="pending",
            cancelled_at=None,
            cancellation_reason=None,
            updated_at=None,
        ),
        SimpleNamespace(
            id=72,
            referral_id=30,
            status="pending",
            cancelled_at=None,
            cancellation_reason=None,
            updated_at=None,
        ),
    ]

    cancelled = await cancel_pending_commission_for_referee(
        FakeSession(source_referral, pending),
        10,
        reason="stripe.subscription_cancelled",
        now=NOW + relativedelta(months=2),
    )

    assert cancelled == pending
    assert [row.status for row in pending] == ["cancelled", "cancelled"]
    assert source_referral.partner_earning_started_at == NOW
    assert source_referral.partner_earning_ends_at == NOW + relativedelta(months=12)
    assert source_referral.retention_streak_started_at is None
    assert source_referral.retention_gate_at is None


@pytest.mark.asyncio
async def test_lapsed_coverage_does_not_blindly_vest_due_commissions():
    due = [
        SimpleNamespace(
            id=81,
            referral_id=30,
            status="pending",
            vests_at=NOW,
            vested_at=None,
            cancelled_at=None,
            cancellation_reason=None,
            updated_at=None,
        ),
        SimpleNamespace(
            id=82,
            referral_id=30,
            status="pending",
            vests_at=NOW,
            vested_at=None,
            cancelled_at=None,
            cancellation_reason=None,
            updated_at=None,
        ),
    ]
    source_referral = referral(
        retention_gate_at=NOW,
        retention_coverage_ends_at=NOW - timedelta(days=30),
    )

    vested = await vest_due_commissions(
        FakeSession(due, [source_referral]),
        now=NOW,
    )

    assert vested == []
    assert [row.status for row in due] == ["cancelled", "cancelled"]
    assert source_referral.retention_streak_started_at is None


@pytest.mark.asyncio
async def test_full_refund_before_gate_cancels_accumulated_pending_commissions():
    source = SimpleNamespace(
        id=91,
        referral_id=30,
        status="pending",
        amount_usd=Decimal("3.80"),
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    sibling = SimpleNamespace(
        id=92,
        referral_id=30,
        status="pending",
        amount_usd=Decimal("3.80"),
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    source_referral = referral()
    session = FakeSession(source, source_referral, [source, sibling])

    action, adjustment = await adjust_commission_for_refund(
        session,
        payment(),
        refund_amount=Decimal("19.00"),
        refunded_total=Decimal("19.00"),
        payment_total=Decimal("19.00"),
        fully_refunded=True,
        reason="customer refund",
        now=NOW + relativedelta(months=1),
    )

    assert action == "cancelled"
    assert adjustment == Decimal("-3.80")
    assert source.status == sibling.status == "cancelled"
    assert source_referral.retention_streak_started_at is None


@pytest.mark.asyncio
async def test_refund_after_gate_qualifies_then_records_manual_adjustment():
    gate = NOW + relativedelta(months=3)
    source = SimpleNamespace(
        id=93,
        referral_id=30,
        status="pending",
        amount_usd=Decimal("3.80"),
        vested_at=None,
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    source_referral = referral(
        retention_gate_at=gate,
        retention_coverage_ends_at=gate + relativedelta(months=1),
    )
    session = FakeSession(source, source_referral, [source])

    action, adjustment = await adjust_commission_for_refund(
        session,
        payment(),
        refund_amount=Decimal("19.00"),
        refunded_total=Decimal("19.00"),
        payment_total=Decimal("19.00"),
        fully_refunded=True,
        reason="late refund",
        now=gate + timedelta(seconds=1),
    )

    assert source_referral.retention_qualified_at == gate
    assert source.status == "vested"
    assert action == "adjusted"
    assert adjustment == Decimal("-3.80")


@pytest.mark.asyncio
async def test_partial_refund_after_gate_is_adjusted_without_reducing_earned_row():
    gate = NOW + relativedelta(months=3)
    source = SimpleNamespace(
        id=94,
        referral_id=30,
        status="pending",
        amount_usd=Decimal("3.80"),
        vests_at=gate,
        vested_at=None,
        cancelled_at=None,
        cancellation_reason=None,
        updated_at=None,
    )
    source_referral = referral(
        retention_gate_at=gate,
        retention_coverage_ends_at=gate + relativedelta(months=1),
    )
    session = FakeSession(source, source_referral, [source])

    action, adjustment = await adjust_commission_for_refund(
        session,
        payment(),
        refund_amount=Decimal("9.50"),
        refunded_total=Decimal("9.50"),
        payment_total=Decimal("19.00"),
        fully_refunded=False,
        reason="late partial refund",
        now=gate + timedelta(seconds=1),
    )

    assert source.status == "vested"
    assert source.amount_usd == Decimal("3.80")
    assert action == "adjusted"
    assert adjustment == Decimal("-1.90")


@pytest.mark.asyncio
async def test_payout_batch_requires_threshold_and_links_vested_commissions():
    # BLK-005 / GK-100: payout threshold is $100; $99.99 must not draft a batch.
    below_threshold = [
        SimpleNamespace(
            id=1,
            referrer_id=99,
            amount_usd=Decimal("99.99"),
            payout_batch_id=None,
        )
    ]
    assert await create_referral_payout_batch(FakeSession(below_threshold), now=NOW) is None
    # The service-level invariant cannot be bypassed by a lower caller value.
    assert (
        await create_referral_payout_batch(
            FakeSession(below_threshold),
            threshold_usd=Decimal("1.00"),
            now=NOW,
        )
        is None
    )

    vested = [
        SimpleNamespace(
            id=2,
            referrer_id=99,
            amount_usd=Decimal("60.00"),
            payout_batch_id=None,
            updated_at=None,
        ),
        SimpleNamespace(
            id=3,
            referrer_id=99,
            amount_usd=Decimal("40.00"),
            payout_batch_id=None,
            updated_at=None,
        ),
    ]
    session = FakeSession(vested)

    batch = await create_referral_payout_batch(session, now=NOW, note="May payout")

    assert isinstance(batch, ReferralPayoutBatch)
    assert batch.status == "draft"
    assert batch.currency == "USD"
    assert batch.threshold_amount == Decimal("100.00")
    assert batch.total_amount == Decimal("100.00")
    assert batch.commission_count == 2
    assert batch.note == "May payout"
    assert [c.payout_batch_id for c in vested] == [batch.id, batch.id]


@pytest.mark.asyncio
async def test_payout_threshold_is_per_partner_and_excludes_ineligible_balances():
    commissions = [
        SimpleNamespace(
            id=1,
            referrer_id=10,
            amount_usd=Decimal("60.00"),
            payout_batch_id=None,
            updated_at=None,
        ),
        SimpleNamespace(
            id=2,
            referrer_id=20,
            amount_usd=Decimal("60.00"),
            payout_batch_id=None,
            updated_at=None,
        ),
    ]
    assert await create_referral_payout_batch(FakeSession(commissions), now=NOW) is None

    commissions.extend(
        [
            SimpleNamespace(
                id=3,
                referrer_id=10,
                amount_usd=Decimal("40.00"),
                payout_batch_id=None,
                updated_at=None,
            ),
            SimpleNamespace(
                id=4,
                referrer_id=30,
                amount_usd=Decimal("100.00"),
                payout_batch_id=None,
                updated_at=None,
            ),
        ]
    )

    batch = await create_referral_payout_batch(FakeSession(commissions), now=NOW)

    assert batch is not None
    assert batch.total_amount == Decimal("200.00")
    assert batch.commission_count == 3
    assert [row.payout_batch_id is not None for row in commissions] == [True, False, True, True]


@pytest.mark.asyncio
async def test_paid_payout_transition_marks_batch_and_commissions_once():
    batch = SimpleNamespace(
        id=700,
        status="sent",
        sent_at=NOW - timedelta(hours=1),
        paid_at=None,
        cancelled_at=None,
        note=None,
    )
    commissions = [
        SimpleNamespace(id=1, status="vested", paid_at=None, updated_at=None),
        SimpleNamespace(id=2, status="vested", paid_at=None, updated_at=None),
    ]
    session = FakeSession(batch, commissions)

    result = await transition_referral_payout_batch(
        session,
        batch.id,
        action="paid",
        admin_id=7,
        tx_hash="0xabc123",
        now=NOW,
    )

    assert result is batch
    assert batch.status == "paid"
    assert batch.paid_at == NOW
    assert "tx_hash=0xabc123" in batch.note
    assert [commission.status for commission in commissions] == ["paid", "paid"]
    assert [commission.paid_at for commission in commissions] == [NOW, NOW]


@pytest.mark.asyncio
async def test_paid_payout_transition_rejects_duplicate_paid_batch():
    batch = SimpleNamespace(id=701, status="paid", note=None)

    with pytest.raises(PayoutTransitionError, match="already paid"):
        await transition_referral_payout_batch(
            FakeSession(batch),
            batch.id,
            action="paid",
            tx_hash="0xabc123",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_cancelled_payout_transition_releases_unpaid_commissions():
    batch = SimpleNamespace(
        id=702,
        status="draft",
        sent_at=None,
        paid_at=None,
        cancelled_at=None,
        note=None,
    )
    commissions = [
        SimpleNamespace(id=1, status="vested", payout_batch_id=batch.id, updated_at=None),
        SimpleNamespace(id=2, status="vested", payout_batch_id=batch.id, updated_at=None),
    ]

    result = await transition_referral_payout_batch(
        FakeSession(batch, commissions),
        batch.id,
        action="cancelled",
        admin_id=7,
        note="wrong wallet",
        now=NOW,
    )

    assert result is batch
    assert batch.status == "cancelled"
    assert batch.cancelled_at == NOW
    assert [commission.payout_batch_id for commission in commissions] == [None, None]
    assert "wrong wallet" in batch.note
