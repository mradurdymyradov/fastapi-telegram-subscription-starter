import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.routers import payments as payments_router
from app.api.routers.payments import RefundRequest, refund_payment
from app.db.models import ReferralAdjustment
from app.payments import refund as refund_module
from app.payments.refund import (
    REFUND_CONFIRMED,
    REFUND_FAILED,
    REFUND_MANUAL_ACTION,
    REFUND_PENDING,
    REFUND_REQUESTED,
    RefundError,
    create_refund,
    resolve_manual_refund,
    sync_provider_refund,
)
from app.services.subscription import end_access_for_refund, has_subscription_access

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


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
        self.flushes = 0
        self._next_id = 900

    async def execute(self, _query):
        return Result(self.results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


class FakeStripeGateway:
    def __init__(self, status="succeeded", refund_id="re_123"):
        self.status = status
        self.refund_id = refund_id
        self.calls = []
        self.retrieve_calls = []

    async def create_refund(self, *, payment_intent_id, charge_id, amount_cents, reason, idempotency_key):
        self.calls.append(
            {
                "payment_intent_id": payment_intent_id,
                "charge_id": charge_id,
                "amount_cents": amount_cents,
                "reason": reason,
                "idempotency_key": idempotency_key,
            }
        )
        return {"id": self.refund_id, "status": self.status}

    async def retrieve_refund(self, refund_id):
        self.retrieve_calls.append(refund_id)
        return {"id": refund_id, "status": self.status}


def make_payment(**overrides):
    data = {
        "id": 20,
        "user_id": 10,
        "provider": "stripe",
        "amount": Decimal("19.00"),
        "currency": "USD",
        "status": "succeeded",
        "refunded_amount": Decimal("0"),
        "is_gift": False,
        "stripe_payment_intent_id": "pi_123",
        "stripe_invoice_id": "in_123",
        "lava_invoice_id": None,
        "provider_event_id": "evt_123",
        "tx_hash": None,
        "external_id": "sub_1",
        "refunds": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_commission(**overrides):
    data = {
        "id": 77,
        "referral_id": 30,
        "status": "pending",
        "amount_usd": Decimal("3.80"),
        "vests_at": NOW + timedelta(days=60),
        "cancelled_at": None,
        "cancellation_reason": None,
        "updated_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_referral(**overrides):
    data = {
        "id": 30,
        "retention_streak_started_at": NOW - timedelta(days=30),
        "retention_coverage_ends_at": NOW + timedelta(days=30),
        "retention_gate_at": NOW + timedelta(days=60),
        "retention_qualified_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_sub(**overrides):
    data = {
        "id": 5,
        "user_id": 10,
        "status": "active",
        "access_revoked_at": None,
        "current_period_end": NOW + timedelta(days=20),
        "expires_at": NOW + timedelta(days=20),
        "grace_started_at": None,
        "grace_ends_at": None,
        "cancel_at_period_end": True,
        "access_revoke_retry_after_at": None,
        "access_revoke_attempts": 0,
        "access_revoke_attempted_at": None,
        "provider": "stripe",
        "provider_status": "active",
        "access_revoke_error": None,
        "invite_link": "Community channel: https://t.me/+channel\nPractice chat: https://t.me/+practice",
        "user": SimpleNamespace(tg_id=10010),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture(autouse=True)
def _patch_settings_and_dispatch(monkeypatch):
    monkeypatch.setattr(
        refund_module,
        "settings",
        SimpleNamespace(stripe_secret_key="sk_test"),
    )

    async def _noop_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(refund_module, "dispatch", _noop_dispatch)


# ─── Stripe (auto, by payment intent) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_stripe_full_refund_marks_refunded_cancels_commission_and_ends_access():
    payment = make_payment()
    commission = make_commission()
    sub = make_sub()
    # end_access query, commission lookup, referral lock, then all streak rows.
    session = FakeSession([sub], commission, make_referral(), [commission])
    gateway = FakeStripeGateway(status="succeeded")

    result = await create_refund(
        session,
        payment,
        amount=None,
        reason="customer request",
        admin_id=7,
        stripe_gateway=gateway,
        now=NOW,
    )

    assert gateway.calls[0]["payment_intent_id"] == "pi_123"
    assert gateway.calls[0]["amount_cents"] == 1900
    assert payment.status == "refunded"
    assert payment.refunded_amount == Decimal("19.00")
    assert result.refund.status == REFUND_CONFIRMED
    assert result.refund.accounting_applied_at == NOW
    assert result.refund.refund_type == "full"
    assert result.refund.provider_refund_id == "re_123"
    assert result.refund.is_manual is False
    assert result.fully_refunded is True
    # pending commission cancelled before vesting
    assert commission.status == "cancelled"
    assert result.commission_action == "cancelled"
    assert result.commission_adjustment_usd == Decimal("-3.80")
    adjustments = [obj for obj in session.added if isinstance(obj, ReferralAdjustment)]
    assert adjustments and adjustments[0].amount_usd == Decimal("-3.80")
    # access ended immediately: window collapsed, returned for the Telegram kick
    assert result.revoked_subscriptions == [sub]
    assert sub.status == "cancelled"
    assert sub.current_period_end == NOW
    assert sub.expires_at == NOW
    assert sub.grace_ends_at is None
    assert has_subscription_access(sub, NOW + timedelta(seconds=1)) is False


@pytest.mark.asyncio
async def test_stripe_partial_refund_keeps_succeeded_and_reduces_commission():
    payment = make_payment()
    commission = make_commission()
    session = FakeSession(commission)
    gateway = FakeStripeGateway(status="succeeded")

    result = await create_refund(
        session,
        payment,
        amount=Decimal("9.50"),
        reason="partial",
        admin_id=7,
        stripe_gateway=gateway,
        now=NOW,
    )

    assert gateway.calls[0]["amount_cents"] == 950
    assert payment.status == "succeeded"
    assert payment.refunded_amount == Decimal("9.50")
    assert result.refund.refund_type == "partial"
    assert result.fully_refunded is False
    # commission reduced to 20% of remaining basis (19.00 - 9.50 = 9.50 → 1.90)
    assert commission.amount_usd == Decimal("1.90")
    assert result.commission_action == "reduced"
    assert result.commission_adjustment_usd == Decimal("-1.90")
    # partial refund does NOT end access
    assert result.revoked_subscriptions == []


@pytest.mark.asyncio
async def test_stripe_refund_after_vesting_records_adjustment_not_cancel():
    payment = make_payment()
    commission = make_commission(status="vested", vests_at=NOW - timedelta(days=1))
    # end_access query (no active subs) runs first, then the commission lookup.
    session = FakeSession([], commission)
    gateway = FakeStripeGateway(status="succeeded")

    result = await create_refund(
        session,
        payment,
        amount=None,
        reason="late refund",
        admin_id=7,
        stripe_gateway=gateway,
        now=NOW,
    )

    assert payment.status == "refunded"
    # vested commission is earned (BLK-005): not reversed, only an adjustment recorded
    assert commission.status == "vested"
    assert result.commission_action == "adjusted"
    assert result.commission_adjustment_usd == Decimal("-3.80")
    adjustments = [obj for obj in session.added if isinstance(obj, ReferralAdjustment)]
    assert adjustments and adjustments[0].amount_usd == Decimal("-3.80")


@pytest.mark.asyncio
async def test_stripe_pending_gateway_status_leaves_payment_unchanged():
    payment = make_payment()
    session = FakeSession()  # no commission lookup happens before provider confirmation
    gateway = FakeStripeGateway(status="pending")

    result = await create_refund(
        session,
        payment,
        amount=None,
        reason="customer",
        admin_id=7,
        stripe_gateway=gateway,
        now=NOW,
    )

    assert result.refund.status == REFUND_PENDING
    assert payment.status == "succeeded"
    assert payment.refunded_amount == Decimal("0")
    assert result.fully_refunded is False
    assert result.commission_action == "none"


@pytest.mark.asyncio
async def test_requested_state_is_durable_without_accounting_effects():
    payment = make_payment()
    session = FakeSession()

    result = await create_refund(
        session,
        payment,
        amount=Decimal("5.00"),
        reason="review first",
        admin_id=7,
        process=False,
        now=NOW,
    )

    assert result.refund.status == REFUND_REQUESTED
    assert result.refund.request_key.startswith("gk-refund-payment-20-1-")
    assert payment.refunded_amount == Decimal("0")
    assert payment.status == "succeeded"
    assert result.refund.accounting_applied_at is None


@pytest.mark.asyncio
async def test_pending_stripe_sync_confirms_once_and_duplicate_sync_is_noop():
    payment = make_payment(is_gift=True, gift_recipient_id=None)
    session = FakeSession()
    gateway = FakeStripeGateway(status="pending")

    requested = await create_refund(
        session,
        payment,
        amount=Decimal("5.00"),
        reason="partial",
        admin_id=7,
        stripe_gateway=gateway,
        now=NOW,
    )
    assert requested.refund.status == REFUND_PENDING
    assert payment.refunded_amount == Decimal("0")

    gateway.status = "succeeded"
    confirmed = await sync_provider_refund(
        session,
        payment,
        requested.refund,
        stripe_gateway=gateway,
        now=NOW,
    )
    assert gateway.retrieve_calls == ["re_123"]
    assert confirmed.refund.status == REFUND_CONFIRMED
    assert payment.refunded_amount == Decimal("5.00")

    replay = await sync_provider_refund(
        session,
        payment,
        requested.refund,
        stripe_gateway=gateway,
        now=NOW + timedelta(minutes=1),
    )
    assert replay.state_changed is False
    assert payment.refunded_amount == Decimal("5.00")
    assert gateway.retrieve_calls == ["re_123"]


@pytest.mark.asyncio
async def test_failed_stripe_refund_does_not_change_accounting_or_access():
    payment = make_payment()
    gateway = FakeStripeGateway(status="failed")

    result = await create_refund(
        FakeSession(),
        payment,
        reason="customer",
        admin_id=7,
        stripe_gateway=gateway,
        now=NOW,
    )

    assert result.refund.status == REFUND_FAILED
    assert payment.status == "succeeded"
    assert payment.refunded_amount == Decimal("0")
    assert result.revoked_subscriptions == []


@pytest.mark.asyncio
async def test_unresolved_refund_reserves_payment_and_blocks_second_request():
    payment = make_payment()
    first = await create_refund(
        FakeSession(),
        payment,
        amount=Decimal("10.00"),
        reason="first",
        admin_id=7,
        stripe_gateway=FakeStripeGateway(status="pending"),
        now=NOW,
    )
    assert first.refund.status == REFUND_PENDING

    with pytest.raises(RefundError, match="resolve it before requesting another"):
        await create_refund(
            FakeSession(),
            payment,
            amount=Decimal("9.00"),
            reason="second",
            admin_id=8,
            stripe_gateway=FakeStripeGateway(status="succeeded"),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_stripe_refund_requires_payment_intent_on_record():
    payment = make_payment(stripe_payment_intent_id=None)
    with pytest.raises(RefundError, match="payment intent"):
        await create_refund(FakeSession(), payment, reason="x", admin_id=7, now=NOW)


@pytest.mark.asyncio
async def test_force_manual_stripe_skips_gateway():
    payment = make_payment()
    session = FakeSession([], None)  # consumed only after explicit confirmation
    gateway = FakeStripeGateway()

    result = await create_refund(
        session,
        payment,
        amount=None,
        reason="already refunded in Stripe dashboard",
        admin_id=7,
        manual=True,
        stripe_gateway=gateway,
        now=NOW,
    )

    assert gateway.calls == []
    assert result.refund.is_manual is True
    assert result.refund.provider_refund_id is None
    assert result.refund.status == REFUND_MANUAL_ACTION
    assert payment.status == "succeeded"
    assert payment.refunded_amount == Decimal("0")

    confirmed = await resolve_manual_refund(
        session,
        payment,
        result.refund,
        target_status=REFUND_CONFIRMED,
        reason="Stripe dashboard refund re_999 verified",
        provider_reference="re_999",
        admin_id=8,
        now=NOW,
    )
    assert confirmed.refund.status == REFUND_CONFIRMED
    assert confirmed.refund.provider_refund_id == "re_999"
    assert confirmed.refund.confirmed_by_admin_id == 8
    assert payment.status == "refunded"


# ─── USDT / Zelle (manual, audit reason required) ───────────────────────────


@pytest.mark.asyncio
async def test_usdt_refund_requires_audit_reason():
    payment = make_payment(provider="usdt", stripe_payment_intent_id=None, tx_hash="0xabc")
    with pytest.raises(RefundError, match="audit reason"):
        await create_refund(FakeSession(), payment, amount=None, reason="  ", admin_id=7, now=NOW)


@pytest.mark.asyncio
async def test_usdt_manual_refund_requires_explicit_confirmation():
    payment = make_payment(provider="usdt", stripe_payment_intent_id=None, tx_hash="0xabc")
    session = FakeSession([], None)

    result = await create_refund(
        session,
        payment,
        amount=None,
        reason="sent TRC20 refund tx 0xdef",
        admin_id=7,
        now=NOW,
    )

    assert result.refund.is_manual is True
    assert result.refund.status == REFUND_MANUAL_ACTION
    assert result.refund.provider_refund_id is None
    assert payment.status == "succeeded"
    assert payment.refunded_amount == Decimal("0")
    assert result.commission_action == "none"

    confirmed = await resolve_manual_refund(
        session,
        payment,
        result.refund,
        target_status=REFUND_CONFIRMED,
        reason="TRC20 transfer confirmed on-chain",
        provider_reference="0xdef",
        admin_id=9,
        now=NOW,
    )
    assert confirmed.refund.status == REFUND_CONFIRMED
    assert confirmed.refund.provider_refund_id == "0xdef"
    assert payment.status == "refunded"

    replay = await resolve_manual_refund(
        session,
        payment,
        result.refund,
        target_status=REFUND_CONFIRMED,
        reason="duplicate callback",
        provider_reference="0xdef",
        admin_id=9,
        now=NOW + timedelta(minutes=1),
    )
    assert replay.state_changed is False
    assert payment.refunded_amount == Decimal("19.00")


@pytest.mark.asyncio
async def test_manual_refund_can_fail_without_accounting_effects():
    payment = make_payment(provider="usdt", stripe_payment_intent_id=None, tx_hash="0xabc")
    session = FakeSession()
    requested = await create_refund(
        session,
        payment,
        amount=Decimal("5.00"),
        reason="operator queued transfer",
        admin_id=7,
        now=NOW,
    )

    failed = await resolve_manual_refund(
        session,
        payment,
        requested.refund,
        target_status=REFUND_FAILED,
        reason="wallet transfer rejected",
        admin_id=7,
        now=NOW,
    )
    assert failed.refund.status == REFUND_FAILED
    assert failed.refund.failure_reason == "wallet transfer rejected"
    assert payment.refunded_amount == Decimal("0")
    assert payment.status == "succeeded"


@pytest.mark.asyncio
async def test_zelle_manual_partial_refund_keeps_succeeded():
    payment = make_payment(provider="zelle", stripe_payment_intent_id=None)
    session = FakeSession(None)

    result = await create_refund(
        session,
        payment,
        amount=Decimal("5.00"),
        reason="partial Zelle refund",
        admin_id=7,
        now=NOW,
    )

    assert result.refund.is_manual is True
    assert result.refund.refund_type == "partial"
    assert payment.status == "succeeded"
    assert payment.refunded_amount == Decimal("0")

    await resolve_manual_refund(
        session,
        payment,
        result.refund,
        target_status=REFUND_CONFIRMED,
        reason="bank transfer confirmed",
        provider_reference="bank-42",
        admin_id=7,
        now=NOW,
    )
    assert payment.status == "succeeded"
    assert payment.refunded_amount == Decimal("5.00")


# ─── Lava (manual only — there is no Lava refund API) ───────────────────────


@pytest.mark.asyncio
async def test_lava_refund_says_there_is_no_lava_refund_api():
    # GK-445: the old message said "not enabled", which reads as "somebody can
    # turn it on". Nobody can — Lava publishes no refund endpoint. The message
    # has to tell an admin what to actually do instead.
    payment = make_payment(provider="lava", stripe_payment_intent_id=None, lava_invoice_id="lav_1")
    with pytest.raises(RefundError, match="no refund API") as exc:
        await create_refund(FakeSession(), payment, amount=None, reason="x", admin_id=7, now=NOW)
    assert "Lava dashboard" in str(exc.value)


def test_the_panel_never_offers_an_automatic_lava_refund():
    """GK-445: the retired flag's only live effect was `_refund_mode` returning
    "auto" for Lava, i.e. a button that answered 400 every time. Nothing in the
    settings can bring it back — the mode does not consult them for Lava."""
    payment = make_payment(provider="lava", stripe_payment_intent_id=None, lava_invoice_id="lav_1")

    assert payments_router._refund_mode(payment) == "manual"
    assert "enable_lava_live_refund" not in inspect.getsource(payments_router._refund_mode)


@pytest.mark.asyncio
async def test_lava_manual_refund_records_when_manual():
    payment = make_payment(provider="lava", stripe_payment_intent_id=None, lava_invoice_id="lav_1")
    session = FakeSession([], None)

    result = await create_refund(
        session,
        payment,
        amount=None,
        reason="refunded in Lava dashboard",
        admin_id=7,
        manual=True,
        now=NOW,
    )

    assert result.refund.is_manual is True
    assert result.refund.status == REFUND_MANUAL_ACTION
    assert payment.status == "succeeded"

    await resolve_manual_refund(
        session,
        payment,
        result.refund,
        target_status=REFUND_CONFIRMED,
        reason="Lava dashboard shows refund complete",
        provider_reference="lava-ref-1",
        admin_id=7,
        now=NOW,
    )
    assert payment.status == "refunded"


# ─── Guards ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cannot_refund_non_succeeded_payment():
    payment = make_payment(status="pending")
    with pytest.raises(RefundError, match="succeeded"):
        await create_refund(FakeSession(), payment, reason="x", admin_id=7, now=NOW)


@pytest.mark.asyncio
async def test_cannot_refund_already_fully_refunded():
    payment = make_payment(refunded_amount=Decimal("19.00"))
    with pytest.raises(RefundError, match="already fully refunded"):
        await create_refund(FakeSession(), payment, reason="x", admin_id=7, now=NOW)


@pytest.mark.asyncio
async def test_refund_amount_cannot_exceed_remaining():
    payment = make_payment(refunded_amount=Decimal("15.00"))
    with pytest.raises(RefundError, match="exceeds"):
        await create_refund(
            FakeSession(),
            payment,
            amount=Decimal("10.00"),
            reason="x",
            admin_id=7,
            now=NOW,
        )


def test_payment_api_refund_summary_exposes_reserved_and_stable_state_contract():
    active = SimpleNamespace(
        id=81,
        amount=Decimal("5.00"),
        currency="USD",
        refund_type="partial",
        status=REFUND_MANUAL_ACTION,
        provider_status="manual_action_required",
        provider_refund_id=None,
        is_manual=True,
        reason="operator action",
        failure_reason=None,
        confirmed_at=None,
        accounting_applied_at=None,
        created_at=NOW,
    )
    payment = make_payment(refunded_amount=Decimal("4.00"), refunds=[active])

    state, pending, remaining, rows = payments_router._refund_summary(payment)

    assert state == REFUND_MANUAL_ACTION
    assert pending == Decimal("5.00")
    assert remaining == Decimal("10.00")
    assert payments_router._refundable(payment) is False
    assert rows[0].status == REFUND_MANUAL_ACTION
    assert rows[0].accounting_applied is False


@pytest.mark.asyncio
async def test_gift_refund_skips_commission_but_ends_recipient_access():
    payment = make_payment(is_gift=True, gift_recipient_id=11)
    sub = make_sub(id=6, user_id=11, user=SimpleNamespace(tg_id=11011))
    # gifts skip the commission lookup; only the end_access query runs.
    session = FakeSession([sub])
    gateway = FakeStripeGateway(status="succeeded")

    result = await create_refund(
        session,
        payment,
        amount=None,
        reason="gift refund",
        admin_id=7,
        stripe_gateway=gateway,
        now=NOW,
    )

    assert payment.status == "refunded"
    assert result.commission_action == "none"
    assert not [obj for obj in session.added if isinstance(obj, ReferralAdjustment)]
    # gift recipient's access is ended
    assert result.revoked_subscriptions == [sub]
    assert sub.status == "cancelled"


# ─── Route (mocked DB + service) ────────────────────────────────────────────


class RouteResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class RouteDB:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _query):
        return RouteResult(self.values.pop(0))


@pytest.mark.asyncio
async def test_refund_route_returns_response_and_audits(monkeypatch):
    payment = make_payment()
    admin = SimpleNamespace(id=7)
    request = SimpleNamespace()
    audits = []

    fake_refund_row = SimpleNamespace(
        id=901,
        amount=Decimal("19.00"),
        refund_type="full",
        status=REFUND_CONFIRMED,
        is_manual=False,
        accounting_applied_at=NOW,
    )
    fake_result = SimpleNamespace(
        refund=fake_refund_row,
        payment_status="refunded",
        fully_refunded=True,
        refunded_total=Decimal("19.00"),
        commission_action="cancelled",
        commission_adjustment_usd=Decimal("-3.80"),
        revoked_subscriptions=[],
    )

    async def fake_create_refund(db, p, **kwargs):
        assert p is payment
        assert kwargs["amount"] is None
        assert kwargs["admin_id"] == admin.id
        return fake_result

    async def fake_audit(db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token=""))
    monkeypatch.setattr(payments_router, "create_refund", fake_create_refund)
    monkeypatch.setattr(payments_router, "audit_record", fake_audit)

    result = await refund_payment(
        payment.id,
        RefundRequest(amount=None, reason="customer", manual=False),
        RouteDB(payment),
        admin,
        request,
    )

    assert result.ok is True
    assert result.refund_id == 901
    assert result.payment_status == "refunded"
    assert result.fully_refunded is True
    assert result.commission_action == "cancelled"
    assert result.commission_adjustment_usd == -3.80
    assert result.access_ended is False
    assert result.access_revoked_count == 0
    assert result.notify_delivered is False
    assert audits[0]["action"] == "payment.refund.request"
    assert audits[0]["target_id"] == payment.id
    assert audits[0]["details"]["commission_action"] == "cancelled"


@pytest.mark.asyncio
async def test_refund_route_kicks_revoked_subscribers(monkeypatch):
    payment = make_payment()
    admin = SimpleNamespace(id=7)
    request = SimpleNamespace()
    sub = make_sub()
    kicks = []
    revoked_invites = []
    records = []

    fake_refund_row = SimpleNamespace(
        id=902,
        amount=Decimal("19.00"),
        refund_type="full",
        status=REFUND_CONFIRMED,
        is_manual=False,
        accounting_applied_at=NOW,
    )
    fake_result = SimpleNamespace(
        refund=fake_refund_row,
        payment_status="refunded",
        fully_refunded=True,
        refunded_total=Decimal("19.00"),
        commission_action="cancelled",
        commission_adjustment_usd=Decimal("-3.80"),
        revoked_subscriptions=[sub],
    )

    async def fake_create_refund(db, p, **kwargs):
        return fake_result

    async def fake_audit(db, **kwargs):
        return None

    async def fake_kick(bot, tg_id):
        kicks.append(tg_id)
        return SimpleNamespace(success=True, retry_after=None, error=None)

    async def fake_revoke_invites(bot, invite_link):
        revoked_invites.append(invite_link)
        return SimpleNamespace(success=True, retry_after=None, error=None)

    def fake_record(s, **kwargs):
        records.append((s, kwargs))

    class FakeBot:
        def __init__(self, token):
            self.token = token
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            return None

    async def fake_notify(*_a, **_k):
        return False

    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token="tkn"))
    monkeypatch.setattr(payments_router, "create_refund", fake_create_refund)
    monkeypatch.setattr(payments_router, "audit_record", fake_audit)
    monkeypatch.setattr(payments_router, "Bot", FakeBot)
    monkeypatch.setattr(payments_router, "_notify_payment_refunded", fake_notify)
    monkeypatch.setattr("app.services.channel_access.kick_user", fake_kick)
    monkeypatch.setattr("app.services.channel_access.revoke_invite_links", fake_revoke_invites)
    monkeypatch.setattr("app.services.subscription.record_access_revoke_attempt", fake_record)

    result = await refund_payment(
        payment.id,
        RefundRequest(amount=None, reason="customer", manual=False),
        RouteDB(payment),
        admin,
        request,
    )

    assert kicks == [10010]
    assert revoked_invites == [sub.invite_link]
    assert len(records) == 1 and records[0][1]["success"] is True
    assert result.access_ended is True
    assert result.access_revoked_count == 1


@pytest.mark.asyncio
async def test_revoke_refunded_channel_access_continues_after_one_crash(monkeypatch):
    first = make_sub(id=1, user=SimpleNamespace(tg_id=10010), invite_link="first")
    second = make_sub(id=2, user=SimpleNamespace(tg_id=10011), invite_link="second")
    calls = []
    records = []

    async def fake_revoke_access(_bot, tg_id, invite_link):
        calls.append((tg_id, invite_link))
        if tg_id == 10010:
            raise RuntimeError("telegram transport exploded")
        return SimpleNamespace(success=True, retry_after=None, error=None)

    def fake_record(s, **kwargs):
        records.append((s.id, kwargs))

    class FakeBot:
        def __init__(self, token):
            self.token = token
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            return None

    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token="tkn"))
    monkeypatch.setattr(payments_router, "Bot", FakeBot)
    monkeypatch.setattr(
        "app.services.channel_access.revoke_subscription_access",
        fake_revoke_access,
    )
    monkeypatch.setattr("app.services.subscription.record_access_revoke_attempt", fake_record)

    count = await payments_router._revoke_refunded_channel_access([first, second])

    assert count == 1
    assert calls == [(10010, "first"), (10011, "second")]
    assert records[0][0] == 1
    assert records[0][1]["success"] is False
    assert "telegram transport exploded" in records[0][1]["error"]
    assert records[1][0] == 2
    assert records[1][1]["success"] is True


@pytest.mark.asyncio
async def test_end_access_for_refund_collapses_window_and_returns_subs():
    sub = make_sub()
    session = FakeSession([sub])

    ended = await end_access_for_refund(session, 10, reason="full refund", now=NOW)

    assert ended == [sub]
    assert sub.status == "cancelled"
    assert sub.current_period_end == NOW
    assert sub.expires_at == NOW
    assert sub.grace_ends_at is None
    assert sub.cancel_at_period_end is False
    assert sub.provider_status == "refunded"
    assert has_subscription_access(sub, NOW + timedelta(seconds=1)) is False


@pytest.mark.asyncio
async def test_end_access_for_refund_no_active_subs_returns_empty():
    session = FakeSession([])
    assert await end_access_for_refund(session, 999, reason="x", now=NOW) == []
