"""GK-377: autorenew cancellation must reach the provider, or say it did not.

The regression these guard against: the bot used to tell every member
"Администратор отключит продление у платёжного провайдера" while making no
provider call at all. Two curators were charged again after asking to stop.
"""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.routers import webhooks_in
from app.bot.handlers import subscription as subscription_handlers
from app.db.models import SupportMessage
from app.payments.base import PaymentEvent
from app.payments.lava_provider import (
    LavaAPIError,
    LavaCancellationUnavailable,
    LavaProvider,
)
from app.payments.stripe_provider import StripeCancellationError, StripeProvider
from app.services import subscription_cancellation as cancellation
from app.services.subscription_cancellation import (
    CANCEL_MANUAL_REQUIRED,
    CANCEL_PROVIDER_CONFIRMED,
    effective_cancel_state,
    request_autorenew_cancellation,
)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    """Returns the same payment row for any lookup the service performs."""

    def __init__(self, payment=None):
        self.payment = payment
        self.added = []

    async def execute(self, _query):
        return Result(self.payment)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def user(**overrides):
    data = {"id": 10, "tg_id": 10010, "username": "member"}
    data.update(overrides)
    return SimpleNamespace(**data)


def subscription(**overrides):
    data = {
        "id": 50,
        "user_id": 10,
        "plan_id": 20,
        "status": "active",
        "source": "stripe",
        "provider": "stripe",
        "provider_subscription_id": "sub_123",
        "provider_status": "active",
        "expires_at": datetime(2026, 8, 6, tzinfo=UTC),
        "current_period_end": datetime(2026, 8, 6, tzinfo=UTC),
        "cancel_at_period_end": False,
        "cancel_state": None,
        "cancel_requested_at": None,
        "cancel_confirmed_at": None,
        "cancel_failure_reason": None,
        "cancel_resolved_at": None,
        "cancel_resolved_by_admin_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def lava_subscription(**overrides):
    data = {
        "source": "lava",
        "provider": "lava",
        # The real contract id from curator Pirogov's 06.07 purchase.
        "provider_subscription_id": "8eecb051-3a6e-4130-9efa-5e5add66ca26",
    }
    data.update(overrides)
    return subscription(**data)


def payment(**overrides):
    data = {
        "id": 777,
        "user_id": 10,
        "provider": "lava",
        "note": "buyer_email=curator@example.com",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


# --------------------------------------------------------------------------
# Provider dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stripe_cancellation_calls_provider_and_preserves_access(monkeypatch):
    calls = []

    async def fake_cancel(subscription_id):
        calls.append(subscription_id)

    monkeypatch.setattr(StripeProvider, "cancel_autorenew", fake_cancel)
    sub = subscription()
    session = FakeSession()

    outcome = await request_autorenew_cancellation(session, sub, user=user())

    assert calls == ["sub_123"], "Stripe must actually be told to stop charging"
    assert outcome.provider_confirmed
    assert sub.cancel_state == CANCEL_PROVIDER_CONFIRMED
    assert sub.cancel_confirmed_at is not None
    assert sub.cancel_failure_reason is None
    # Access is preserved: cancel-at-period-end, not an immediate revoke.
    assert sub.cancel_at_period_end is True
    assert sub.status == "active"
    assert sub.expires_at == datetime(2026, 8, 6, tzinfo=UTC)
    assert session.added == [], "a confirmed cancellation needs no manual queue item"


@pytest.mark.asyncio
async def test_lava_cancellation_with_contract_id_calls_provider(monkeypatch):
    calls = []

    async def fake_cancel(contract_id, *, email=None):
        calls.append((contract_id, email))

    monkeypatch.setattr(LavaProvider, "cancel_autorenew", fake_cancel)
    sub = lava_subscription()

    outcome = await request_autorenew_cancellation(FakeSession(payment()), sub, user=user())

    # Lava requires the buyer email beside the contract id, and it is not the
    # member's account email — it comes off the note written at checkout.
    assert calls == [("8eecb051-3a6e-4130-9efa-5e5add66ca26", "curator@example.com")]
    assert outcome.provider_confirmed
    assert sub.cancel_state == CANCEL_PROVIDER_CONFIRMED
    assert sub.cancel_at_period_end is True


@pytest.mark.asyncio
async def test_unknown_contract_id_falls_back_to_manual_queue(monkeypatch):
    """The normal RUB case: Lava sent no purchase webhook, so we hold no id."""
    called = False

    async def fake_cancel(contract_id, *, email=None):
        nonlocal called
        called = True
        raise LavaCancellationUnavailable("Lava contract id is unknown")

    monkeypatch.setattr(LavaProvider, "cancel_autorenew", fake_cancel)
    sub = lava_subscription(provider_subscription_id=None)
    session = FakeSession(payment())

    outcome = await request_autorenew_cancellation(session, sub, user=user())

    assert called
    assert outcome.needs_manual_action
    assert not outcome.provider_confirmed
    assert sub.cancel_state == CANCEL_MANUAL_REQUIRED
    assert sub.cancel_requested_at is not None
    assert sub.cancel_confirmed_at is None
    # The critical bit: we must NOT claim the provider stopped charging.
    assert sub.cancel_at_period_end is False
    assert effective_cancel_state(sub) == CANCEL_MANUAL_REQUIRED


@pytest.mark.asyncio
async def test_manual_queue_item_carries_buyer_email_and_payment_id(monkeypatch):
    async def fake_cancel(contract_id, *, email=None):
        raise LavaCancellationUnavailable("Lava contract id is unknown")

    monkeypatch.setattr(LavaProvider, "cancel_autorenew", fake_cancel)
    session = FakeSession(payment())

    await request_autorenew_cancellation(
        session,
        lava_subscription(provider_subscription_id=None),
        user=user(),
    )

    assert len(session.added) == 1
    item = session.added[0]
    assert isinstance(item, SupportMessage)
    assert item.user_id == 10
    # Identifiers a human needs to find the contract in the Lava dashboard.
    assert "buyer_email=curator@example.com" in item.content
    assert "payment_id=777" in item.content
    assert "tg_id=10010" in item.content
    assert "РУЧНАЯ ОТМЕНА" in item.content


@pytest.mark.asyncio
async def test_lava_gate_off_routes_to_manual_without_calling_provider(monkeypatch):
    """With the live flag off, nothing is sent to Lava and nothing is claimed."""
    monkeypatch.setattr(
        cancellation.LavaProvider,
        "cancel_autorenew",
        LavaProvider.cancel_autorenew,
    )
    monkeypatch.setattr(
        "app.payments.lava_provider.settings",
        SimpleNamespace(
            enable_lava_autorenew_cancellation=False,
            lava_api_key="k",
            lava_api_base_url="https://gate.lava.top",
        ),
    )
    sub = lava_subscription()

    outcome = await request_autorenew_cancellation(FakeSession(payment()), sub, user=user())

    assert outcome.needs_manual_action
    assert sub.cancel_at_period_end is False
    assert "not enabled" in (sub.cancel_failure_reason or "")


@pytest.mark.asyncio
async def test_stripe_gate_off_makes_no_network_call(monkeypatch):
    """Both providers ship gated off; an ungated path once reached api.stripe.com."""
    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("Stripe must not be contacted while the gate is off")

    monkeypatch.setattr("app.payments.stripe_provider.stripe.Subscription.modify", explode)
    monkeypatch.setattr(
        "app.payments.stripe_provider.settings",
        SimpleNamespace(
            enable_stripe_autorenew_cancellation=False,
            stripe_secret_key="sk_test_x",
        ),
    )
    sub = subscription()

    outcome = await request_autorenew_cancellation(FakeSession(payment()), sub, user=user())

    assert outcome.needs_manual_action
    assert sub.cancel_at_period_end is False
    assert "not enabled" in (sub.cancel_failure_reason or "")


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_manual(monkeypatch):
    async def fake_cancel(subscription_id):
        raise StripeCancellationError("No such subscription: sub_123")

    monkeypatch.setattr(StripeProvider, "cancel_autorenew", fake_cancel)
    sub = subscription()

    outcome = await request_autorenew_cancellation(FakeSession(payment()), sub, user=user())

    assert outcome.needs_manual_action
    assert sub.cancel_state == CANCEL_MANUAL_REQUIRED
    assert "No such subscription" in sub.cancel_failure_reason
    assert sub.cancel_at_period_end is False


@pytest.mark.asyncio
async def test_lava_api_error_falls_back_to_manual(monkeypatch):
    async def fake_cancel(contract_id, *, email=None):
        raise LavaAPIError("Lava cancel-subscription returned HTTP 500")

    monkeypatch.setattr(LavaProvider, "cancel_autorenew", fake_cancel)
    sub = lava_subscription()

    outcome = await request_autorenew_cancellation(FakeSession(payment()), sub, user=user())

    assert outcome.needs_manual_action
    assert "HTTP 500" in sub.cancel_failure_reason


@pytest.mark.asyncio
async def test_provider_without_cancellation_api_goes_manual():
    sub = subscription(provider="usdt", source="usdt", provider_subscription_id=None)

    outcome = await request_autorenew_cancellation(FakeSession(payment()), sub, user=user())

    assert outcome.needs_manual_action
    assert "usdt" in sub.cancel_failure_reason


# --------------------------------------------------------------------------
# Duplicate clicks / idempotency
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_click_after_success_does_not_call_provider_again(monkeypatch):
    calls = []

    async def fake_cancel(subscription_id):
        calls.append(subscription_id)

    monkeypatch.setattr(StripeProvider, "cancel_autorenew", fake_cancel)
    sub = subscription()
    session = FakeSession()

    first = await request_autorenew_cancellation(session, sub, user=user())
    second = await request_autorenew_cancellation(session, sub, user=user())

    assert len(calls) == 1, "a confirmed cancellation must not be re-sent"
    assert first.provider_confirmed and second.provider_confirmed
    assert second.already_recorded is True


@pytest.mark.asyncio
async def test_duplicate_click_after_manual_fallback_queues_once(monkeypatch):
    async def fake_cancel(contract_id, *, email=None):
        raise LavaCancellationUnavailable("Lava contract id is unknown")

    monkeypatch.setattr(LavaProvider, "cancel_autorenew", fake_cancel)
    sub = lava_subscription(provider_subscription_id=None)
    session = FakeSession(payment())

    await request_autorenew_cancellation(session, sub, user=user())
    first_requested_at = sub.cancel_requested_at
    second = await request_autorenew_cancellation(session, sub, user=user())

    assert len(session.added) == 1, "one queue item per member, not one per click"
    assert second.already_recorded is True
    assert sub.cancel_requested_at == first_requested_at


@pytest.mark.asyncio
async def test_retry_after_manual_fallback_can_still_succeed(monkeypatch):
    """A retry once the provider is reachable upgrades the row to confirmed."""
    outcomes = iter([LavaCancellationUnavailable("gated off"), None])

    async def fake_cancel(contract_id, *, email=None):
        result = next(outcomes)
        if result is not None:
            raise result

    monkeypatch.setattr(LavaProvider, "cancel_autorenew", fake_cancel)
    sub = lava_subscription()
    session = FakeSession(payment())

    await request_autorenew_cancellation(session, sub, user=user())
    assert sub.cancel_state == CANCEL_MANUAL_REQUIRED

    outcome = await request_autorenew_cancellation(session, sub, user=user())

    assert outcome.provider_confirmed
    assert sub.cancel_state == CANCEL_PROVIDER_CONFIRMED
    assert sub.cancel_failure_reason is None
    assert sub.cancel_at_period_end is True


# --------------------------------------------------------------------------
# Webhook reconciliation
# --------------------------------------------------------------------------


class WebhookDB:
    def __init__(self, sub):
        self.sub = sub

    async def execute(self, _query):
        return Result(self.sub)

    def add(self, _obj):
        return None

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_lava_cancellation_webhook_confirms_a_manual_queue_item(monkeypatch):
    """An admin cancelling in the Lava dashboard closes the queue item itself."""
    monkeypatch.setattr(webhooks_in, "cancel_pending_commission_for_referee", AsyncMock())
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", AsyncMock())

    sub = lava_subscription(
        cancel_state=CANCEL_MANUAL_REQUIRED,
        cancel_requested_at=datetime(2026, 7, 22, tzinfo=UTC),
        cancel_failure_reason="Lava contract id is unknown",
    )
    assert effective_cancel_state(sub) == CANCEL_MANUAL_REQUIRED

    event = PaymentEvent(
        external_id="evt_1",
        status="subscription_deleted",
        amount=0,
        currency="RUB",
        metadata={
            "lava_subscription_id": "8eecb051-3a6e-4130-9efa-5e5add66ca26",
            "lava_contract_status": "cancelled",
            "lava_period_end": "2026-08-06T00:00:00+00:00",
        },
    )

    await webhooks_in._handle_lava_subscription_event(WebhookDB(sub), event)

    assert effective_cancel_state(sub) == CANCEL_PROVIDER_CONFIRMED
    assert not cancellation.needs_manual_cancellation(sub)


@pytest.mark.asyncio
async def test_lava_cancellation_webhook_is_idempotent(monkeypatch):
    monkeypatch.setattr(webhooks_in, "cancel_pending_commission_for_referee", AsyncMock())
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", AsyncMock())

    sub = lava_subscription(cancel_state=CANCEL_MANUAL_REQUIRED)
    event = PaymentEvent(
        external_id="evt_1",
        status="subscription_deleted",
        amount=0,
        currency="RUB",
        metadata={
            "lava_subscription_id": "8eecb051-3a6e-4130-9efa-5e5add66ca26",
            "lava_contract_status": "cancelled",
            "lava_period_end": "2026-08-06T00:00:00+00:00",
        },
    )
    db = WebhookDB(sub)

    await webhooks_in._handle_lava_subscription_event(db, event)
    first = (effective_cancel_state(sub), sub.expires_at, sub.status)
    await webhooks_in._handle_lava_subscription_event(db, event)

    assert (effective_cancel_state(sub), sub.expires_at, sub.status) == first


@pytest.mark.asyncio
async def test_stripe_subscription_webhook_confirms_cancellation(monkeypatch):
    monkeypatch.setattr(webhooks_in, "cancel_pending_commission_for_referee", AsyncMock())
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", AsyncMock())

    sub = subscription(cancel_state=CANCEL_MANUAL_REQUIRED)
    event = PaymentEvent(
        external_id="evt_2",
        status="subscription_updated",
        amount=0,
        currency="USD",
        metadata={
            "stripe_subscription_id": "sub_123",
            "stripe_subscription_status": "active",
            "stripe_subscription_cancel_at_period_end": "True",
        },
    )

    await webhooks_in._handle_stripe_subscription_event(WebhookDB(sub), event)

    assert sub.cancel_at_period_end is True
    assert effective_cancel_state(sub) == CANCEL_PROVIDER_CONFIRMED


def test_cancellation_made_outside_the_bot_is_reflected():
    """Cancelled straight in the provider dashboard — we never recorded a thing."""
    sub = subscription(cancel_state=None, provider_status="cancelled")
    assert effective_cancel_state(sub) == CANCEL_PROVIDER_CONFIRMED

    sub = subscription(cancel_state=None, cancel_at_period_end=True)
    assert effective_cancel_state(sub) == CANCEL_PROVIDER_CONFIRMED


def test_no_cancellation_on_record_is_none():
    assert effective_cancel_state(subscription()) is None
    assert not cancellation.has_cancellation_on_record(subscription())


def test_resolved_manual_item_leaves_the_queue():
    sub = subscription(cancel_state=CANCEL_MANUAL_REQUIRED)
    assert cancellation.needs_manual_cancellation(sub)

    sub.cancel_resolved_at = datetime(2026, 7, 23, tzinfo=UTC)
    assert not cancellation.needs_manual_cancellation(sub)


# --------------------------------------------------------------------------
# Bot copy — the member must never be told a charge stopped when it did not
# --------------------------------------------------------------------------


def callback(data: str):
    return SimpleNamespace(data=data, message=AsyncMock(), answer=AsyncMock())


@pytest.mark.asyncio
async def test_bot_confirms_only_when_the_provider_confirmed(monkeypatch):
    async def fake_cancel(subscription_id):
        return None

    monkeypatch.setattr(StripeProvider, "cancel_autorenew", fake_cancel)
    sub = subscription()
    monkeypatch.setattr(
        subscription_handlers,
        "get_active_subscription",
        AsyncMock(return_value=sub),
    )

    cb = callback("sub_cancel_confirm")
    await subscription_handlers.subscription_cancel_confirm(
        cb, FakeSession(), AsyncMock(), user()
    )

    text = cb.message.edit_text.await_args.args[0]
    assert "Автопродление отключено" in text
    assert "Списаний больше не будет" in text


@pytest.mark.asyncio
async def test_bot_does_not_claim_success_on_manual_fallback(monkeypatch):
    async def fake_cancel(contract_id, *, email=None):
        raise LavaCancellationUnavailable("Lava contract id is unknown")

    monkeypatch.setattr(LavaProvider, "cancel_autorenew", fake_cancel)
    sub = lava_subscription(provider_subscription_id=None)
    monkeypatch.setattr(
        subscription_handlers,
        "get_active_subscription",
        AsyncMock(return_value=sub),
    )

    cb = callback("sub_cancel_confirm")
    await subscription_handlers.subscription_cancel_confirm(
        cb, FakeSession(payment()), AsyncMock(), user()
    )

    text = cb.message.edit_text.await_args.args[0]
    # The exact regression: the old copy asserted the admin would switch it off.
    assert "Автопродление отключено" not in text
    assert "не подтвердили" in text
    assert "06.08.2026" in text, "access end date must still be shown"


# --------------------------------------------------------------------------
# Admin surface: the manual queue must be visible and closable
# --------------------------------------------------------------------------


class RowsResult:
    def __init__(self, *, scalar=None, rows=None, value=None):
        self._scalar = scalar
        self._rows = rows or []
        self._value = value

    def scalar_one(self):
        return self._scalar

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._value


class AdminDB:
    def __init__(self, *results):
        self.results = list(results)

    async def execute(self, _query):
        return self.results.pop(0)

    def add(self, _obj):
        return None


def admin(**overrides):
    data = {"id": 3, "email": "grant@example.com"}
    data.update(overrides)
    return SimpleNamespace(**data)


def plan_row(**overrides):
    data = {"id": 20, "name": "1 месяц"}
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_admin_queue_exposes_identifiers_for_manual_action():
    from app.api.routers.subscriptions import list_cancellations

    sub = lava_subscription(
        provider_subscription_id=None,
        cancel_state=CANCEL_MANUAL_REQUIRED,
        cancel_requested_at=datetime(2026, 7, 22, tzinfo=UTC),
        cancel_failure_reason="Lava contract id is unknown",
    )
    db = AdminDB(
        RowsResult(scalar=1),
        RowsResult(rows=[(sub, user(), plan_row())]),
        RowsResult(value=payment()),
    )

    page = await list_cancellations(db, admin(), open_only=True, limit=50, offset=0)

    assert page.total == 1
    row = page.items[0]
    assert row.cancel_state == CANCEL_MANUAL_REQUIRED
    assert row.provider_subscription_id is None
    assert row.buyer_email == "curator@example.com"
    assert row.payment_id == 777
    assert row.tg_id == 10010


@pytest.mark.asyncio
async def test_admin_resolve_marks_item_handled_without_claiming_provider_proof(monkeypatch):
    from app.api.routers import subscriptions as subs_router

    monkeypatch.setattr(subs_router, "audit_record", AsyncMock())
    sub = lava_subscription(cancel_state=CANCEL_MANUAL_REQUIRED)
    db = AdminDB(RowsResult(value=sub))

    result = await subs_router.resolve_cancellation(
        50,
        subs_router.ResolveCancellationIn(note="отменено в панели Lava"),
        db,
        admin(),
        None,
    )

    assert result["ok"] is True
    assert sub.cancel_resolved_at is not None
    assert sub.cancel_resolved_by_admin_id == 3
    # Resolving is an admin assertion, not provider proof.
    assert sub.cancel_state == CANCEL_MANUAL_REQUIRED
    assert sub.cancel_at_period_end is False
    assert not cancellation.needs_manual_cancellation(sub)


@pytest.mark.asyncio
async def test_admin_resolve_is_idempotent(monkeypatch):
    from app.api.routers import subscriptions as subs_router

    monkeypatch.setattr(subs_router, "audit_record", AsyncMock())
    sub = lava_subscription(
        cancel_state=CANCEL_MANUAL_REQUIRED,
        cancel_resolved_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    db = AdminDB(RowsResult(value=sub))

    result = await subs_router.resolve_cancellation(
        50, subs_router.ResolveCancellationIn(), db, admin(), None
    )

    assert result["already_resolved"] is True


@pytest.mark.asyncio
async def test_admin_resolve_rejects_subscription_without_a_request(monkeypatch):
    from fastapi import HTTPException

    from app.api.routers import subscriptions as subs_router

    monkeypatch.setattr(subs_router, "audit_record", AsyncMock())
    db = AdminDB(RowsResult(value=lava_subscription()))

    with pytest.raises(HTTPException) as exc:
        await subs_router.resolve_cancellation(
            50, subs_router.ResolveCancellationIn(), db, admin(), None
        )
    assert exc.value.status_code == 400


def test_backfilled_legacy_request_reads_as_manual_not_confirmed():
    """Migration 0022 demotes pre-GK-377 flags; they must not read as confirmed.

    The old handler set cancel_at_period_end without calling any provider, so a
    legacy row carrying it is not proof of anything. The migration clears the
    flag and sets manual_required — this asserts the resulting shape surfaces in
    the queue rather than telling the member the charge was stopped.
    """
    backfilled = lava_subscription(
        cancel_state=CANCEL_MANUAL_REQUIRED,
        cancel_at_period_end=False,
        provider_status="active",
        cancel_requested_at=datetime(2026, 7, 16, tzinfo=UTC),
        cancel_failure_reason="Backfilled by GK-377: ...never confirmed with the provider.",
    )

    assert effective_cancel_state(backfilled) == CANCEL_MANUAL_REQUIRED
    assert cancellation.needs_manual_cancellation(backfilled)

    # A row the provider genuinely confirmed is left alone by the backfill.
    confirmed = lava_subscription(provider_status="cancelled", cancel_at_period_end=True)
    assert effective_cancel_state(confirmed) == CANCEL_PROVIDER_CONFIRMED
    assert not cancellation.needs_manual_cancellation(confirmed)
