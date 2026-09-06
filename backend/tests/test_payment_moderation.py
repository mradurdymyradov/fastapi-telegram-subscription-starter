from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.routers import payments as payments_router
from app.api.routers.payments import ManualDecision, list_payments, moderate_manual


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDB:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _query):
        return ScalarResult(self.values.pop(0))


class ListingResult:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one(self):
        return self.scalar

    def all(self):
        return self.rows


class ListingDB:
    def __init__(self):
        self.statements = []
        self.results = [ListingResult(scalar=0), ListingResult(rows=[])]

    async def execute(self, query):
        self.statements.append(query)
        return self.results.pop(0)


def make_payment(**overrides):
    data = {
        "id": 123,
        "user_id": 10,
        "gift_recipient_id": None,
        "provider": "manual",
        "amount": Decimal("19.00"),
        "currency": "USD",
        "status": "awaiting_review",
        "approved_at": None,
        "approved_by": None,
        "note": None,
        "is_gift": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_list_payments_can_focus_an_arbitrary_historical_payment():
    db = ListingDB()

    page = await list_payments(
        db,
        SimpleNamespace(id=7),
        status=None,
        provider=None,
        payment_id=987,
        limit=100,
        offset=0,
    )

    assert page.items == []
    assert page.total == 0
    count_sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    rows_sql = str(db.statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "payments.id = 987" in count_sql
    assert "payments.id = 987" in rows_sql


@pytest.mark.asyncio
async def test_manual_approve_routes_through_fulfillment_without_presetting_marker(monkeypatch):
    payment = make_payment()
    admin = SimpleNamespace(id=7)
    request = SimpleNamespace()
    audit_calls = []
    fulfillment_calls = []

    async def fake_fulfill(session, bot, approved_payment):
        fulfillment_calls.append((session, bot, approved_payment.approved_at))
        assert approved_payment is payment
        assert approved_payment.status == "succeeded"
        assert approved_payment.approved_by == admin.id
        approved_payment.approved_at = datetime(2026, 5, 31, tzinfo=UTC)
        return SimpleNamespace(id=55, invite_link="invite-link")

    async def fake_audit_record(db, **kwargs):
        audit_calls.append((db, kwargs))

    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token=""))
    monkeypatch.setattr(payments_router, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(payments_router, "audit_record", fake_audit_record)

    result = await moderate_manual(
        payment.id,
        ManualDecision(decision="approve"),
        FakeDB(payment),
        admin,
        request,
    )

    assert result == {"ok": True, "status": "succeeded", "notify_delivered": False}
    assert len(fulfillment_calls) == 1
    _session, bot, pre_fulfillment_marker = fulfillment_calls[0]
    assert bot is None
    assert pre_fulfillment_marker is None
    assert payment.approved_at == datetime(2026, 5, 31, tzinfo=UTC)
    assert len(audit_calls) == 1
    assert audit_calls[0][1]["action"] == "payment.approve"
    assert audit_calls[0][1]["target_id"] == payment.id
    assert audit_calls[0][1]["details"] == {
        "amount": 19.0,
        "currency": "USD",
        "notify_ok": False,
    }


@pytest.mark.asyncio
async def test_manual_approve_uses_billing_notification_template(monkeypatch):
    payment = make_payment()
    admin = SimpleNamespace(id=7)
    request = SimpleNamespace()
    subscription = SimpleNamespace(id=55, invite_link="invite-link")
    notifications = []
    audits = []

    async def fake_fulfill(_session, _bot, approved_payment):
        approved_payment.approved_at = datetime(2026, 5, 31, tzinfo=UTC)
        return subscription

    async def fake_notify(session, approved_payment, notified_subscription):
        notifications.append((session, approved_payment, notified_subscription))
        return True

    async def fake_audit_record(db, **kwargs):
        audits.append((db, kwargs))

    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token="token"))
    monkeypatch.setattr(payments_router, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(payments_router, "notify_payment_succeeded", fake_notify)
    monkeypatch.setattr(payments_router, "audit_record", fake_audit_record)

    class FakeBot:
        def __init__(self, token):
            self.token = token
            self.session = SimpleNamespace(close=self.close)

        async def close(self):
            return None

    monkeypatch.setattr(payments_router, "Bot", FakeBot)
    db = FakeDB(payment)

    result = await moderate_manual(
        payment.id,
        ManualDecision(decision="approve"),
        db,
        admin,
        request,
    )

    assert result == {"ok": True, "status": "succeeded", "notify_delivered": True}
    assert notifications == [(db, payment, subscription)]
    assert audits[0][1]["details"]["notify_ok"] is True
