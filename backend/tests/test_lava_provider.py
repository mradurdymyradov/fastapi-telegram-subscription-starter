import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from app.api.routers import webhooks_in
from app.payments import lava_provider
from app.payments.base import PaymentEvent
from app.payments.lava_provider import (
    LavaAPIError,
    LavaCancellationUnavailable,
    LavaCheckoutUnavailable,
    LavaProvider,
    _fetch_offer_listing,
    _post_lava_invoice,
)
from app.services.subscription_cancellation import buyer_email_from_payment


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDB:
    def __init__(self, *values):
        self.values = list(values)
        self.added = []
        self.flushes = 0
        self.rolled_back = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for i, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = 900 + i

    async def execute(self, _query):
        if not self.values:
            raise AssertionError("FakeDB.execute called without queued result")
        return ScalarResult(self.values.pop(0))

    async def rollback(self):
        self.rolled_back = True


class FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

    @property
    def headers(self):
        return {"content-length": str(len(self._body))}

    async def stream(self):
        yield self._body

    async def body(self):
        return self._body


@pytest.fixture
def lava_settings(monkeypatch):
    settings = SimpleNamespace(
        lava_webhook_auth_mode="api_key",
        lava_webhook_api_key="hook-secret",
        lava_webhook_basic_username="lava-user",
        lava_webhook_basic_password="lava-pass",
        enable_lava_live_checkout=False,
        enable_lava_autorenew_cancellation=False,
        enable_lava_rub_offer_page=False,
        lava_api_key="",
        lava_api_base_url="https://gate.lava.top",
        lava_offer_id="",
        public_base_url="https://example.test",
        bot_token="",
    )
    monkeypatch.setattr(lava_provider, "settings", settings)
    monkeypatch.setattr(webhooks_in, "settings", settings)
    return settings


@pytest.fixture(autouse=True)
def lava_billing_notifications_noop(monkeypatch):
    async def noop(*_args, **_kwargs):
        return True

    monkeypatch.setattr(webhooks_in, "notify_payment_succeeded", noop)
    monkeypatch.setattr(webhooks_in, "notify_payment_failed", noop)
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", noop)


def lava_payload(**overrides) -> bytes:
    payload = {
        "eventType": "payment.success",
        "product": {"id": "product_1", "title": "membership_saas"},
        "contractId": "pay_1",
        "buyer": {"email": "buyer@example.com"},
        "amount": 19.0,
        "currency": "USD",
        "timestamp": "2026-05-31T00:00:00Z",
        "status": "subscription-active",
        "clientUtm": {"utm_content": "payment_123"},
        "errorMessage": "",
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode()


def make_lava_payment(**overrides):
    data = {
        "id": 123,
        "user_id": 10,
        "plan_id": 20,
        "provider": "lava",
        "amount": Decimal("19.00"),
        "currency": "USD",
        "status": "pending",
        "approved_at": None,
        "provider_event_id": None,
        "external_id": None,
        "lava_invoice_id": None,
        "lava_subscription_id": None,
        "is_renewal": False,
        "is_gift": False,
        "gift_recipient_id": None,
        "billing_period_start": None,
        "billing_period_end": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_lava_subscription(**overrides):
    data = {
        "id": 50,
        "user_id": 10,
        "plan_id": 20,
        "status": "active",
        "provider": "lava",
        "provider_subscription_id": "lava_sub_1",
        "provider_status": "active",
        "current_period_start": None,
        "current_period_end": datetime(2026, 6, 30, tzinfo=UTC),
        "expires_at": datetime(2026, 6, 30, tzinfo=UTC),
        "cancel_at_period_end": False,
        "grace_started_at": None,
        "grace_ends_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_lava_parse_payment_success_extracts_verified_contract(lava_settings):
    assert LavaProvider.verify_webhook_auth(x_api_key="hook-secret") is True

    event = LavaProvider.parse_webhook(lava_payload())

    assert event == PaymentEvent(
        external_id="pay_1",
        status="invoice_paid",
        amount=19.0,
        currency="USD",
        metadata={
            "lava_event_id": "payment.success:pay_1",
            "lava_event_type": "payment.success",
            "lava_invoice_id": "pay_1",
            "lava_subscription_id": "pay_1",
            "lava_raw_sha256": event.metadata["lava_raw_sha256"],
            "lava_is_renewal": "false",
            "lava_contract_status": "subscription-active",
            "payment_id": "123",
            "user_id": "",
            "plan_id": "",
            "is_gift": "",
            "gift_recipient_id": "",
            "lava_period_start": "",
            "lava_period_end": "",
            "lava_cancel_at_period_end": "",
        },
    )


def test_lava_basic_auth_mode(lava_settings):
    lava_settings.lava_webhook_auth_mode = "basic"
    token = base64.b64encode(b"lava-user:lava-pass").decode()

    assert LavaProvider.verify_webhook_auth(authorization=f"Basic {token}") is True
    assert LavaProvider.verify_webhook_auth(authorization="Basic bad") is False


@pytest.mark.asyncio
async def test_lava_webhook_rejects_bad_api_key(lava_settings):
    with pytest.raises(HTTPException) as exc:
        await webhooks_in.lava_webhook(
            FakeRequest(lava_payload()),
            FakeDB(),
            x_api_key="wrong-secret",
            authorization=None,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_lava_duplicate_event_returns_already_without_fulfillment(
    monkeypatch,
    lava_settings,
):
    async def duplicate_record(_db, _event, *, raw_hash):
        assert len(raw_hash) == 64
        return None

    async def fail_fulfill(*_args, **_kwargs):
        raise AssertionError("duplicate Lava events must not re-run fulfillment")

    monkeypatch.setattr(webhooks_in, "_record_lava_event_once", duplicate_record)
    monkeypatch.setattr(webhooks_in, "fulfill_payment", fail_fulfill)

    result = await webhooks_in.lava_webhook(
        FakeRequest(lava_payload()),
        FakeDB(),
        x_api_key="hook-secret",
        authorization=None,
    )

    assert result == {"ok": True, "already": True}


@pytest.mark.asyncio
async def test_lava_initial_payment_fulfills_existing_pending_payment(
    monkeypatch,
    lava_settings,
):
    event = LavaProvider.parse_webhook(lava_payload())
    payment = make_lava_payment()
    record = SimpleNamespace(payment_id=None)
    fulfilled = []

    async def fake_fulfill(_session, _bot, paid):
        fulfilled.append(paid)
        paid.approved_at = datetime(2026, 5, 31, tzinfo=UTC)
        return SimpleNamespace(id=51, invite_link=None)

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)

    result = await webhooks_in._handle_lava_invoice_paid(
        FakeDB(None, payment),
        event,
        record,
    )

    assert result == {"ok": True, "fulfilled": True}
    assert fulfilled == [payment]
    assert record.payment_id == 123
    assert payment.status == "succeeded"
    assert payment.lava_invoice_id == "pay_1"
    assert payment.lava_subscription_id == "pay_1"
    assert payment.external_id == "pay_1"
    assert payment.provider_event_id == "payment.success:pay_1"
    assert payment.billing_period_start is None
    assert payment.billing_period_end is None


@pytest.mark.asyncio
async def test_lava_renewal_creates_local_payment_and_fulfills(
    monkeypatch,
    lava_settings,
):
    raw = lava_payload(
        eventType="subscription.recurring.payment.success",
        contractId="pay_renewal",
        parentContractId="lava_sub_1",
        clientUtm={},
    )
    event = LavaProvider.parse_webhook(raw)
    local_sub = make_lava_subscription()
    record = SimpleNamespace(payment_id=None)
    fulfilled = []
    notified = []
    fulfilled_sub = SimpleNamespace(id=52, invite_link=None)

    async def fake_fulfill(_session, _bot, paid):
        fulfilled.append(paid)
        paid.approved_at = datetime(2026, 6, 30, tzinfo=UTC)
        return fulfilled_sub

    async def fake_notify(session, payment, subscription):
        notified.append((session, payment, subscription))
        return True

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(webhooks_in, "notify_payment_succeeded", fake_notify)
    db = FakeDB(None, None, local_sub)

    result = await webhooks_in._handle_lava_invoice_paid(db, event, record)

    assert result == {"ok": True, "fulfilled": True}
    assert len(fulfilled) == 1
    renewal = fulfilled[0]
    assert renewal in db.added
    assert renewal.id == record.payment_id
    assert renewal.user_id == 10
    assert renewal.plan_id == 20
    assert renewal.is_renewal is True
    assert renewal.lava_invoice_id == "pay_renewal"
    assert renewal.lava_subscription_id == "lava_sub_1"
    assert notified == [(db, renewal, fulfilled_sub)]


@pytest.mark.asyncio
async def test_lava_payment_failed_updates_payment_and_subscription(
    monkeypatch,
    lava_settings,
):
    raw = lava_payload(
        eventType="subscription.recurring.payment.failed",
        contractId="pay_failed",
        parentContractId="lava_sub_1",
        clientUtm={},
        status="subscription-failed",
    )
    event = LavaProvider.parse_webhook(raw)
    payment = make_lava_payment(id=124, lava_invoice_id="pay_failed")
    sub = make_lava_subscription()
    # Paid-through date in the future relative to "now" so grace deterministically
    # starts at the period end. (Replaces a hard-coded 2026-06-30 that became a
    # time-bomb: once real time passed it, max(paid_until, now) picked now.)
    paid_until = datetime.now(UTC).replace(microsecond=0) + timedelta(days=5)
    sub.current_period_end = paid_until
    sub.expires_at = paid_until
    record = SimpleNamespace(payment_id=None)
    notifications = []

    async def fake_notify(session, *, payment, subscription, provider):
        notifications.append((session, payment, subscription, provider))
        return True

    monkeypatch.setattr(webhooks_in, "notify_payment_failed", fake_notify)
    db = FakeDB(payment, sub)
    result = await webhooks_in._handle_lava_invoice_payment_failed(
        db,
        event,
        record,
    )

    assert result == {"ok": True, "updated": True}
    assert record.payment_id == 124
    assert payment.status == "failed"
    assert payment.provider_event_id == "subscription.recurring.payment.failed:pay_failed"
    assert sub.provider_status == "past_due"
    assert sub.grace_started_at == paid_until
    assert notifications == [(db, payment, sub, "lava")]


@pytest.mark.asyncio
async def test_lava_subscription_cancelled_marks_subscription_and_commission(
    monkeypatch,
    lava_settings,
):
    # Paid-through date in the future: cancellation is an auto-renew stop, so
    # access stays active until willExpireAt. Use a now-relative date so the
    # test does not flip behaviour once a fixed date passes.
    will_expire = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    raw = lava_payload(
        eventType="subscription.cancelled",
        contractId="lava_sub_1",
        clientUtm={},
        status=None,
        cancelledAt="2026-06-20T00:00:00Z",
        willExpireAt=will_expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    event = LavaProvider.parse_webhook(raw)
    sub = make_lava_subscription(cancel_at_period_end=True)
    cancellations = []
    notifications = []

    async def fake_cancel_pending_commission(session, referee_id, *, reason):
        cancellations.append((session, referee_id, reason))

    async def fake_notify(session, subscription, *, provider):
        notifications.append((session, subscription, provider))
        return True

    monkeypatch.setattr(
        webhooks_in,
        "cancel_pending_commission_for_referee",
        fake_cancel_pending_commission,
    )
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", fake_notify)
    db = FakeDB(sub)

    result = await webhooks_in._handle_lava_subscription_event(db, event)

    assert result == {"ok": True, "updated": True}
    assert sub.status == "active"
    assert sub.provider_status == "cancelled"
    assert sub.cancel_at_period_end is True
    assert sub.current_period_end == will_expire
    assert sub.expires_at == will_expire
    assert cancellations == [(db, 10, "lava.subscription_cancelled")]
    assert notifications == [(db, sub, "lava")]


@pytest.mark.asyncio
async def test_lava_subscription_cancelled_past_paid_through_revokes_now(
    monkeypatch,
    lava_settings,
):
    # When willExpireAt is already in the past, the cancellation must revoke
    # access immediately (status -> cancelled, no lingering cancel_at_period_end).
    will_expire = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    raw = lava_payload(
        eventType="subscription.cancelled",
        contractId="lava_sub_1",
        clientUtm={},
        status=None,
        willExpireAt=will_expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    event = LavaProvider.parse_webhook(raw)
    sub = make_lava_subscription()

    async def fake_cancel_pending_commission(session, referee_id, *, reason):
        return None

    monkeypatch.setattr(
        webhooks_in,
        "cancel_pending_commission_for_referee",
        fake_cancel_pending_commission,
    )
    db = FakeDB(sub)

    result = await webhooks_in._handle_lava_subscription_event(db, event)

    assert result == {"ok": True, "updated": True}
    assert sub.status == "cancelled"
    assert sub.cancel_at_period_end is False
    assert sub.expires_at == will_expire


def _offer_listing(*prices):
    """Stand in for Lava's `/api/v2/products` catalogue.

    Every live checkout consults it since GK-482 — the invoice payload names no
    amount, so this listing is what decides the charge.
    """

    async def fake_listing(offer_id):
        assert offer_id == "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
        return "b03adb7c-797f-4391-a07b-7dcafd12b1aa", list(prices)

    return fake_listing


def _alert_recorder(monkeypatch):
    """Capture the GK-482 ops alerts instead of reaching for a bot token."""
    sent = []

    async def fake_alert(text, **kwargs):
        sent.append((text, kwargs))
        return True

    monkeypatch.setattr(lava_provider, "send_ops_alert", fake_alert)
    return sent


@pytest.mark.asyncio
async def test_lava_live_checkout_creates_verified_v3_invoice(
    monkeypatch,
    lava_settings,
):
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    user = SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None)
    plan = SimpleNamespace(
        id=20,
        code="6m",
        duration_days=180,
        price_rub=0,
        price_usd=Decimal("89.00"),
    )
    discount = SimpleNamespace(
        amount=Decimal("89.00"),
        applied=False,
        kind=None,
        code=None,
        link_payment=lambda _payment: None,
    )
    payloads = []

    async def fake_discount(*_args, **_kwargs):
        return discount

    async def fake_post(payload):
        payloads.append(payload)
        return {
            "id": "contract_6m",
            "status": "new",
            "amountTotal": {"amount": 89, "currency": "USD"},
            "paymentUrl": "https://pay.lava.top/contract_6m",
        }

    monkeypatch.setattr(lava_provider, "discount_for_checkout", fake_discount)
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fake_post)
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing(
            {"periodicity": "PERIOD_180_DAYS", "currency": "USD", "amount": 89.0}
        ),
    )
    db = FakeDB()

    result = await LavaProvider.create_checkout(
        db,
        user,
        plan,
        buyer_email="buyer@example.com",
    )

    assert result.payment_id == 901
    assert result.url == "https://pay.lava.top/contract_6m"
    assert payloads == [
        {
            "email": "buyer@example.com",
            "offerId": "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5",
            "currency": "USD",
            "periodicity": "PERIOD_180_DAYS",
            "buyerLanguage": "RU",
            "clientUtm": {
                "utm_source": "telegram",
                "utm_medium": "bot",
                "utm_campaign": "membership_saas",
                "utm_content": "payment_901",
            },
        }
    ]
    payment = db.added[0]
    assert payment.lava_invoice_id == "contract_6m"
    assert payment.lava_subscription_id == "contract_6m"
    assert payment.external_id == "contract_6m"


@pytest.mark.asyncio
async def test_lava_live_checkout_accepts_nested_success_response(
    monkeypatch,
    lava_settings,
):
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    buyer = SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None)
    selected_plan = SimpleNamespace(
        id=20,
        code="1m",
        duration_days=30,
        price_rub=0,
        price_usd=Decimal("19.00"),
    )

    async def fake_discount(*_args, **_kwargs):
        return SimpleNamespace(
            amount=Decimal("19.00"),
            applied=False,
            kind=None,
            code=None,
            link_payment=lambda _payment: None,
        )

    async def fake_post(_payload):
        return {
            "data": {
                "contractId": "contract_nested",
                "payment_url": "https://pay.lava.top/nested",
                "amount_total": {"amount": "19.00", "currency": "USD"},
            }
        }

    monkeypatch.setattr(lava_provider, "discount_for_checkout", fake_discount)
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fake_post)
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing({"periodicity": "MONTHLY", "currency": "USD", "amount": 19.0}),
    )
    db = FakeDB()

    result = await LavaProvider.create_checkout(
        db,
        buyer,
        selected_plan,
        buyer_email="buyer@example.com",
    )

    assert result.url == "https://pay.lava.top/nested"
    payment = db.added[0]
    assert payment.lava_invoice_id == "contract_nested"
    assert payment.external_id == "contract_nested"


@pytest.mark.asyncio
async def test_lava_post_invoice_accepts_any_success_status(monkeypatch, lava_settings):
    lava_settings.lava_api_key = "live-api-key"

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "contract_200", "paymentUrl": "https://pay.lava.top/ok"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, path, *, headers, json):
            assert path == "/api/v3/invoice"
            assert headers == {"X-Api-Key": "live-api-key"}
            assert json == {"offerId": "offer_1"}
            return FakeResponse()

    monkeypatch.setattr(lava_provider.httpx, "AsyncClient", FakeClient)

    assert await _post_lava_invoice({"offerId": "offer_1"}) == {
        "id": "contract_200",
        "paymentUrl": "https://pay.lava.top/ok",
    }


@pytest.mark.asyncio
async def test_lava_live_checkout_rejects_fixed_offer_discount(
    monkeypatch,
    lava_settings,
):
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    discount = SimpleNamespace(
        amount=Decimal("15.20"),
        applied=True,
        kind="referral",
        code="monthly_first_invoice_20",
    )

    async def fake_discount(*_args, **_kwargs):
        return discount

    monkeypatch.setattr(lava_provider, "discount_for_checkout", fake_discount)
    db = FakeDB()

    with pytest.raises(LavaCheckoutUnavailable):
        await LavaProvider.create_checkout(
            db,
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=99),
            SimpleNamespace(
                id=20,
                code="1m",
                duration_days=30,
                price_rub=0,
                price_usd=Decimal("19.00"),
            ),
            buyer_email="buyer@example.com",
        )

    assert db.rolled_back is True
    assert db.added == []


# --- GK-418: RUB checkout creates an API invoice (GK-412's offer page is gated off) ---


def _rub_plan(**overrides):
    data = {
        "id": 20,
        "code": "1m",
        "duration_days": 30,
        "price_rub": Decimal("1500.00"),
        "price_usd": Decimal("19.00"),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _no_discount(amount="1500.00"):
    async def fake_discount(*_args, **_kwargs):
        return SimpleNamespace(
            amount=Decimal(amount),
            applied=False,
            kind=None,
            code=None,
            link_payment=lambda _payment: None,
        )

    return fake_discount


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "duration_days", "price_rub", "periodicity"),
    [
        ("1m", 30, "1500.00", "MONTHLY"),
        ("6m", 180, "7000.00", "PERIOD_180_DAYS"),
        ("12m", 365, "10000.00", "PERIOD_YEAR"),
    ],
)
async def test_lava_live_checkout_rub_creates_api_invoice_by_default(
    monkeypatch,
    lava_settings,
    code,
    duration_days,
    price_rub,
    periodicity,
):
    """GK-418: with the flag off, RUB behaves exactly like USD/EUR.

    The contract id must land on the payment at creation time — that is what
    makes the purchase webhook resolvable and the sale visible in Lava's seller
    API. The offer-page path stored neither, which is how a paid member ended up
    with no access and no trace on 2026-07-16.

    GK-482 added the catalogue lookup ahead of the invoice call. It must not
    change where the buyer is sent: this test used to assert the listing was
    never read, and now asserts it is read and that the checkout is otherwise
    byte-for-byte the same.
    """
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    payloads = []
    listing_reads = []

    async def fake_post(payload):
        payloads.append(payload)
        return {
            "id": "contract_rub",
            "status": "new",
            "amountTotal": {"amount": float(price_rub), "currency": "RUB"},
            "paymentUrl": (
                "https://app.lava.top/products/b03adb7c/92cb80cf?paymentParams=eyJ4IjoxfQ"
            ),
        }

    listing = _offer_listing(
        {"periodicity": periodicity, "currency": "RUB", "amount": float(price_rub)}
    )

    async def counting_listing(offer_id):
        listing_reads.append(offer_id)
        return await listing(offer_id)

    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount(price_rub))
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fake_post)
    monkeypatch.setattr(lava_provider, "_fetch_offer_listing", counting_listing)
    db = FakeDB()

    result = await LavaProvider.create_checkout(
        db,
        SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
        _rub_plan(code=code, duration_days=duration_days, price_rub=Decimal(price_rub)),
        buyer_email="buyer@example.com",
    )

    assert payloads == [
        {
            "email": "buyer@example.com",
            "offerId": "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5",
            "currency": "RUB",
            "periodicity": periodicity,
            "buyerLanguage": "RU",
            "clientUtm": {
                "utm_source": "telegram",
                "utm_medium": "bot",
                "utm_campaign": "membership_saas",
                "utm_content": f"payment_{result.payment_id}",
            },
        }
    ]
    assert listing_reads == ["92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"]
    assert result.url.startswith("https://app.lava.top/products/")
    payment = db.added[0]
    assert payment.currency == "RUB"
    assert payment.lava_invoice_id == "contract_rub"
    assert payment.lava_subscription_id == "contract_rub"
    assert payment.external_id == "contract_rub"
    # GK-377 cancels the contract with contractId + buyer email, and reads that
    # email back out of the note — losing it here would silently push every
    # future cancellation into the manual queue.
    assert buyer_email_from_payment(payment) == "buyer@example.com"
    assert db.rolled_back is False


@pytest.mark.asyncio
async def test_lava_live_checkout_rub_invoice_fails_closed_on_total_mismatch(
    monkeypatch,
    lava_settings,
):
    """GK-410's invoice-total guard must still cover RUB after GK-418.

    The catalogue agrees with the plan here, so GK-482's pre-flight check passes
    and the response echo is the thing under test — the second look, not the
    first.
    """
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"

    async def fake_post(_payload):
        return {
            "id": "contract_rub",
            "amountTotal": {"amount": 990, "currency": "RUB"},
            "paymentUrl": "https://app.lava.top/products/b03adb7c/92cb80cf",
        }

    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount())
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fake_post)
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing({"periodicity": "MONTHLY", "currency": "RUB", "amount": 1500.0}),
    )
    db = FakeDB()

    with pytest.raises(LavaAPIError):
        await LavaProvider.create_checkout(
            db,
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(),
            buyer_email="buyer@example.com",
        )

    assert db.rolled_back is True


# --- GK-482: the invoice path bills Lava's catalogue price, so check it first ---


@pytest.mark.asyncio
async def test_lava_invoice_refuses_when_offer_price_drifted(monkeypatch, lava_settings):
    """The failure this guard exists for: Lava would charge 6000, we quoted 7000.

    No invoice may be created — asking Lava to bill a contract we have already
    decided is wrong is how the member gets a payment link for a price nobody
    agreed to.
    """
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    alerts = _alert_recorder(monkeypatch)

    async def fail_post(_payload):
        raise AssertionError("no invoice may be created against a drifted price")

    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount("7000.00"))
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fail_post)
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing(
            {"periodicity": "PERIOD_180_DAYS", "currency": "RUB", "amount": 6000.0}
        ),
    )
    db = FakeDB()

    with pytest.raises(LavaAPIError):
        await LavaProvider.create_checkout(
            db,
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(code="6m", duration_days=180, price_rub=Decimal("7000.00")),
            buyer_email="buyer@example.com",
        )

    assert db.rolled_back is True
    # The bot answers both of this guard's exceptions with "use Stripe or USDT"
    # and pages nobody, so the refusal has to raise the alarm itself or a
    # repricing takes Lava offline in silence.
    assert len(alerts) == 1
    text, kwargs = alerts[0]
    assert "6000.00" in text and "7000.00" in text
    assert kwargs["key"] == "lava_offer_price_drift:PERIOD_180_DAYS:RUB"
    assert kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_lava_invoice_refuses_when_offer_stops_selling_the_period(
    monkeypatch,
    lava_settings,
):
    """A period pulled from the offer is a redirect to Stripe, not a charge."""
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    alerts = _alert_recorder(monkeypatch)

    async def fail_post(_payload):
        raise AssertionError("no invoice may be created for an unsold period")

    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount("10000.00"))
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fail_post)
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing({"periodicity": "MONTHLY", "currency": "RUB", "amount": 1500.0}),
    )
    db = FakeDB()

    with pytest.raises(LavaCheckoutUnavailable):
        await LavaProvider.create_checkout(
            db,
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(code="12m", duration_days=365, price_rub=Decimal("10000.00")),
            buyer_email="buyer@example.com",
        )

    assert db.rolled_back is True
    assert [kwargs["key"] for _text, kwargs in alerts] == [
        "lava_offer_period_missing:PERIOD_YEAR:RUB"
    ]


@pytest.mark.asyncio
async def test_lava_invoice_refuses_when_the_catalogue_cannot_be_read(
    monkeypatch,
    lava_settings,
):
    """Fail closed, not open: an unreadable catalogue proves nothing about price.

    Both calls go to the same host with the same key moments apart, so a
    catalogue that cannot be read is rarely an invoice endpoint that can be
    trusted — and the alternative is billing an unverified amount.

    It also has to page someone. This guard made `/api/v2/products` a hard
    dependency of every RUB checkout, so an hour Lava cannot serve it is an hour
    roubles do not sell — and the member-facing symptom is "use Stripe or USDT",
    which nobody in ops ever sees.
    """
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    alerts = _alert_recorder(monkeypatch)

    async def fail_post(_payload):
        raise AssertionError("no invoice may be created without a verified price")

    async def broken_listing(_offer_id):
        raise LavaAPIError("Lava products returned HTTP 503")

    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount())
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fail_post)
    monkeypatch.setattr(lava_provider, "_fetch_offer_listing", broken_listing)
    db = FakeDB()

    with pytest.raises(LavaAPIError):
        await LavaProvider.create_checkout(
            db,
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(),
            buyer_email="buyer@example.com",
        )

    assert db.rolled_back is True
    assert len(alerts) == 1
    text, kwargs = alerts[0]
    assert "HTTP 503" in text
    # One bucket for every cause: during an outage every checkout raises, and the
    # signal worth an hourly message is "roubles are not selling", not which of
    # the four ways the listing failed this time.
    assert kwargs["key"] == "lava_offer_lookup_failed"
    assert kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_a_missing_offer_id_alerts_on_the_same_channel_as_an_outage(
    monkeypatch,
    lava_settings,
):
    """The config-shaped cause of the same refusal: our offer id is not listed.

    A deleted or renamed offer on Lava's side reads identically to a 503 from
    here — the catalogue answered and our offer was not in it — and it also stops
    RUB checkout dead. Same alert, and `detail` is what tells the two apart.
    """
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    alerts = _alert_recorder(monkeypatch)

    async def fail_post(_payload):
        raise AssertionError("no invoice may be created without a verified price")

    async def offer_absent(_offer_id):
        raise LavaAPIError("Configured Lava offer was not found in the products list")

    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount())
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fail_post)
    monkeypatch.setattr(lava_provider, "_fetch_offer_listing", offer_absent)

    with pytest.raises(LavaAPIError):
        await LavaProvider.create_checkout(
            FakeDB(),
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(),
            buyer_email="buyer@example.com",
        )

    assert len(alerts) == 1
    text, kwargs = alerts[0]
    assert "was not found in the products list" in text
    assert "LAVA_OFFER_ID" in text
    assert kwargs["key"] == "lava_offer_lookup_failed"


@pytest.mark.asyncio
async def test_an_undeliverable_outage_alert_still_refuses_the_checkout(
    monkeypatch,
    lava_settings,
):
    """The alert is best-effort; the refusal is not.

    If paging ops could turn a refused checkout into an unrelated crash, the
    alert would be a liability on the payment path rather than an aid — the
    member would get a generic error instead of "use Stripe or USDT".
    """
    lava_settings.enable_lava_live_checkout = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"

    async def exploding_alert(_text, **_kwargs):
        raise RuntimeError("no bot token in this environment")

    async def broken_listing(_offer_id):
        raise LavaAPIError("Lava products request failed")

    monkeypatch.setattr(lava_provider, "send_ops_alert", exploding_alert)
    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount())
    monkeypatch.setattr(lava_provider, "_fetch_offer_listing", broken_listing)

    with pytest.raises(LavaAPIError, match="Lava products request failed"):
        await LavaProvider.create_checkout(
            FakeDB(),
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(),
            buyer_email="buyer@example.com",
        )


@pytest.mark.asyncio
async def test_lava_live_checkout_rub_links_to_offer_page_with_utm(
    monkeypatch,
    lava_settings,
):
    lava_settings.enable_lava_live_checkout = True
    lava_settings.enable_lava_rub_offer_page = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"

    async def fail_post(_payload):
        raise AssertionError("RUB checkout must not create an API invoice")

    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount())
    monkeypatch.setattr(lava_provider, "_post_lava_invoice", fail_post)
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing(
            {"periodicity": "MONTHLY", "currency": "RUB", "amount": 1500.0},
            {"periodicity": "MONTHLY", "currency": "USD", "amount": 19.0},
        ),
    )
    db = FakeDB()

    result = await LavaProvider.create_checkout(
        db,
        SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
        _rub_plan(),
        buyer_email="buyer@example.com",
    )

    parsed = urlparse(result.url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "app.lava.top"
    assert parsed.path == (
        "/products/b03adb7c-797f-4391-a07b-7dcafd12b1aa"
        "/92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"
    )
    query = parse_qs(parsed.query)
    assert query["currency"] == ["RUB"]
    assert query["language"] == ["ru"]
    assert query["utm_source"] == ["telegram"]
    assert query["utm_medium"] == ["bot"]
    assert query["utm_campaign"] == ["membership_saas"]
    assert query["utm_content"] == [f"payment_{result.payment_id}"]

    payment = db.added[0]
    assert payment.lava_invoice_id is None
    assert payment.external_id is None
    assert payment.note == "buyer_email=buyer@example.com"
    assert db.rolled_back is False


@pytest.mark.asyncio
async def test_lava_live_checkout_rub_unavailable_when_period_not_on_offer(
    monkeypatch,
    lava_settings,
):
    lava_settings.enable_lava_live_checkout = True
    lava_settings.enable_lava_rub_offer_page = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"

    _alert_recorder(monkeypatch)
    monkeypatch.setattr(
        lava_provider, "discount_for_checkout", _no_discount("7000.00")
    )
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing({"periodicity": "MONTHLY", "currency": "RUB", "amount": 1500.0}),
    )
    db = FakeDB()

    with pytest.raises(LavaCheckoutUnavailable):
        await LavaProvider.create_checkout(
            db,
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(id=21, code="6m", duration_days=180, price_rub=Decimal("7000.00")),
            buyer_email="buyer@example.com",
        )

    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_lava_live_checkout_rub_fails_closed_on_price_drift(
    monkeypatch,
    lava_settings,
):
    lava_settings.enable_lava_live_checkout = True
    lava_settings.enable_lava_rub_offer_page = True
    lava_settings.lava_api_key = "live-api-key"
    lava_settings.lava_offer_id = "92cb80cf-89d0-4f87-8c00-9dc15fddc1c5"

    _alert_recorder(monkeypatch)
    monkeypatch.setattr(lava_provider, "discount_for_checkout", _no_discount())
    monkeypatch.setattr(
        lava_provider,
        "_fetch_offer_listing",
        _offer_listing({"periodicity": "MONTHLY", "currency": "RUB", "amount": 990.0}),
    )
    db = FakeDB()

    with pytest.raises(LavaAPIError):
        await LavaProvider.create_checkout(
            db,
            SimpleNamespace(id=10, tg_id=100010, language="ru", referrer_id=None),
            _rub_plan(),
            buyer_email="buyer@example.com",
        )

    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_fetch_offer_listing_finds_offer_and_prices(monkeypatch, lava_settings):
    lava_settings.lava_api_key = "live-api-key"

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "items": [
                    {
                        "data": {
                            "id": "prod-other",
                            "offers": [{"id": "offer-other", "prices": []}],
                        }
                    },
                    {
                        "data": {
                            "id": "prod-1",
                            "offers": [
                                {
                                    "id": "offer-1",
                                    "prices": [
                                        {
                                            "periodicity": "MONTHLY",
                                            "currency": "RUB",
                                            "amount": 1500.0,
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, path, *, params, headers):
            assert path == "/api/v2/products"
            assert params == {"showAllSubscriptionPeriods": "true"}
            assert headers == {"X-Api-Key": "live-api-key"}
            return FakeResponse()

    monkeypatch.setattr(lava_provider.httpx, "AsyncClient", FakeClient)

    product_id, prices = await _fetch_offer_listing("offer-1")

    assert product_id == "prod-1"
    assert prices == [{"periodicity": "MONTHLY", "currency": "RUB", "amount": 1500.0}]

    with pytest.raises(LavaAPIError):
        await _fetch_offer_listing("offer-missing")


@pytest.mark.asyncio
async def test_lava_paid_amount_mismatch_keeps_unbound_payment_pending(
    monkeypatch,
    lava_settings,
):
    # A page-flow payment (no lava_invoice_id yet) correlated via clientUtm
    # must not be fulfilled when the paid total differs from the recorded one.
    raw = lava_payload(amount=387.76, currency="RUB")
    event = LavaProvider.parse_webhook(raw)
    payment = make_lava_payment(amount=Decimal("1500.00"), currency="RUB")
    record = SimpleNamespace(payment_id=None)

    async def fail_fulfill(*_args, **_kwargs):
        raise AssertionError("must not fulfill on amount mismatch")

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fail_fulfill)

    result = await webhooks_in._handle_lava_invoice_paid(
        FakeDB(None, payment),
        event,
        record,
    )

    assert result == {"ok": True, "amount_mismatch": True}
    assert record.payment_id == 123
    assert payment.status == "pending"
    assert payment.lava_invoice_id is None


@pytest.mark.asyncio
async def test_lava_paid_amount_guard_skips_invoice_bound_payment(
    monkeypatch,
    lava_settings,
):
    # Invoice-flow payments were amount-validated at creation; the paid event
    # is matched by the exact contract id and still fulfills.
    event = LavaProvider.parse_webhook(lava_payload(amount=15.0))
    payment = make_lava_payment(lava_invoice_id="pay_1", amount=Decimal("19.00"))
    record = SimpleNamespace(payment_id=None)
    fulfilled = []

    async def fake_fulfill(_session, _bot, paid):
        fulfilled.append(paid)
        paid.approved_at = datetime(2026, 7, 15, tzinfo=UTC)
        return SimpleNamespace(id=53, invite_link=None)

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)

    result = await webhooks_in._handle_lava_invoice_paid(
        FakeDB(payment),
        event,
        record,
    )

    assert result == {"ok": True, "fulfilled": True}
    assert fulfilled == [payment]


@pytest.mark.asyncio
async def test_lava_unmatched_paid_event_pages_a_human(monkeypatch, lava_settings):
    """A charge we cannot match is money taken with nothing granted (GK-416).

    Lava bills recurring contracts on its own schedule. If the contract id does
    not resolve to a local subscription — the ``provider_subscription_id`` was
    never stored, or the sale came through a platform offer page — nothing in
    this handler can fulfill it. We still answer 200 so Lava stops retrying, and
    that is exactly why silence here is dangerous: the member is charged, gets
    no access, and no one finds out until they complain.
    """
    raw = lava_payload(
        eventType="subscription.recurring.payment.success",
        contractId="pay_orphan",
        parentContractId="lava_sub_unknown",
        clientUtm={},
        amount=1500.0,
        currency="RUB",
    )
    event = LavaProvider.parse_webhook(raw)
    record = SimpleNamespace(payment_id=None)
    alerts = []

    async def fake_alert(text, **kwargs):
        alerts.append((text, kwargs))
        return True

    async def fail_fulfill(*_args, **_kwargs):
        raise AssertionError("nothing is fulfillable without a local subscription")

    monkeypatch.setattr(webhooks_in, "send_ops_alert", fake_alert)
    monkeypatch.setattr(webhooks_in, "fulfill_payment", fail_fulfill)

    result = await webhooks_in._handle_lava_invoice_paid(
        FakeDB(None, None, None),
        event,
        record,
    )

    assert result == {"ok": True, "ignored": True}
    assert len(alerts) == 1
    text, kwargs = alerts[0]
    # The alert must carry what a human needs to find the charge in Lava and
    # match it to a member by hand.
    assert "lava_sub_unknown" in text
    assert "pay_orphan" in text
    assert "1500" in text
    assert "RUB" in text
    assert kwargs["severity"] == "error"
    # Bucketed per contract, so a second orphaned contract still alerts.
    assert "pay_orphan" in kwargs["key"]


@pytest.mark.asyncio
async def test_lava_amount_mismatch_pages_a_human(monkeypatch, lava_settings):
    """The mismatch branch parks the payment for review — say so out loud."""
    raw = lava_payload(amount=387.76, currency="RUB")
    event = LavaProvider.parse_webhook(raw)
    payment = make_lava_payment(amount=Decimal("1500.00"), currency="RUB")
    record = SimpleNamespace(payment_id=None)
    alerts = []

    async def fake_alert(text, **kwargs):
        alerts.append((text, kwargs))
        return True

    async def fail_fulfill(*_args, **_kwargs):
        raise AssertionError("must not fulfill on amount mismatch")

    monkeypatch.setattr(webhooks_in, "send_ops_alert", fake_alert)
    monkeypatch.setattr(webhooks_in, "fulfill_payment", fail_fulfill)

    result = await webhooks_in._handle_lava_invoice_paid(
        FakeDB(None, payment),
        event,
        record,
    )

    assert result == {"ok": True, "amount_mismatch": True}
    assert len(alerts) == 1
    text, _kwargs = alerts[0]
    assert "123" in text  # the parked payment id, for the admin queue


@pytest.mark.asyncio
async def test_lava_successful_payment_sends_no_ops_alert(monkeypatch, lava_settings):
    """The alert must mean something — it cannot fire on the normal path."""
    event = LavaProvider.parse_webhook(lava_payload())
    payment = make_lava_payment()
    record = SimpleNamespace(payment_id=None)
    alerts = []

    async def fake_alert(text, **kwargs):
        alerts.append((text, kwargs))
        return True

    async def fake_fulfill(_session, _bot, paid):
        paid.approved_at = datetime(2026, 5, 31, tzinfo=UTC)
        return SimpleNamespace(id=51, invite_link=None)

    monkeypatch.setattr(webhooks_in, "send_ops_alert", fake_alert)
    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)

    result = await webhooks_in._handle_lava_invoice_paid(
        FakeDB(None, payment),
        event,
        record,
    )

    assert result == {"ok": True, "fulfilled": True}
    assert alerts == []


@pytest.mark.asyncio
async def test_lava_cancel_autorenew_calls_documented_endpoint(monkeypatch, lava_settings):
    """Cancellation is DELETE /api/v1/subscriptions with contractId AND email.

    Lava's spec defines `/api/v1/subscriptions/{id}` for GET only, so addressing
    the contract in the path answers 404 — which `cancel_autorenew` reports as
    "Lava does not know this contract". That is indistinguishable from the
    legitimate platform-sale case, so every cancellation would look like an
    honest manual-queue fallback while never reaching Lava at all.
    """
    lava_settings.enable_lava_autorenew_cancellation = True
    lava_settings.lava_api_key = "live-api-key"
    calls = []

    class FakeResponse:
        status_code = 204

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def delete(self, path, *, params, headers):
            calls.append((path, params, headers))
            return FakeResponse()

    monkeypatch.setattr(lava_provider.httpx, "AsyncClient", FakeClient)

    await LavaProvider.cancel_autorenew(
        "8eecb051-3a6e-4130-9efa-5e5add66ca26",
        email="aleksey_pirogoff@example.com",
    )

    assert calls == [
        (
            "/api/v1/subscriptions",
            {
                "contractId": "8eecb051-3a6e-4130-9efa-5e5add66ca26",
                "email": "aleksey_pirogoff@example.com",
            },
            {"X-Api-Key": "live-api-key"},
        )
    ]


@pytest.mark.asyncio
async def test_lava_cancel_autorenew_without_email_never_calls_lava(monkeypatch, lava_settings):
    """email is required by the API, so an unaddressable cancellation is manual.

    Attempting it would fail anyway; the point is that it fails as
    LavaCancellationUnavailable — the member is told a human must act, instead
    of being told the charge was stopped.
    """
    lava_settings.enable_lava_autorenew_cancellation = True
    lava_settings.lava_api_key = "live-api-key"

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("Lava must not be contacted without a buyer email")

    monkeypatch.setattr(lava_provider.httpx, "AsyncClient", explode)

    with pytest.raises(LavaCancellationUnavailable):
        await LavaProvider.cancel_autorenew(
            "8eecb051-3a6e-4130-9efa-5e5add66ca26",
            email=None,
        )


# --- Renewal path, pinned against the official payload shape (GK-417) ---------
#
# No contract in the live Lava account has ever renewed, so the ~05.08.2026 charge
# on the second curator's contract will be the first recurring event this
# integration has ever received. These two tests pin the assumptions that charge
# depends on, taken from the official spec example
# (gate.lava.top/docs/documentation.yaml, successful_subscription_recurring_webhook_payload):
# a recurring charge carries a NEW contractId and repeats the first purchase's id
# in parentContractId.

# The live contract deliberately left active as the renewal test. Its first
# purchase is payment #61, already succeeded, with lava_invoice_id == this uuid.
LIVE_PARENT_CONTRACT = "7982991d-7015-4a2a-9388-43e1b52137b2"
# Shape of the per-charge id Lava mints for the renewal (spec example value).
RENEWAL_CHARGE_CONTRACT = "d41db415-ad71-4f2a-8d8c-27eefee91e66"


@pytest.mark.asyncio
async def test_lava_renewal_matches_parent_contract_and_never_rebinds_first_payment(
    monkeypatch,
    lava_settings,
):
    """A recurring charge must open a NEW payment, not re-touch the first one.

    The failure this guards against is silent and costs the member their access:
    if the renewal resolved back to the original purchase, that payment already
    has ``approved_at`` set, so ``_handle_lava_invoice_paid`` would answer
    ``{"already": True}`` — no extension, no alert, nothing. The member is
    charged 1500 ₽ and quietly loses the subscription they just paid to keep.

    The payload here is the worst realistic case: clientUtm still carries the
    FIRST payment's ``utm_content`` (Lava echoes the checkout's UTMs), so the
    only thing keeping the renewal off payment #61 is the renewal guard in
    ``_find_lava_payment_for_invoice``.
    """
    raw = lava_payload(
        eventType="subscription.recurring.payment.success",
        contractId=RENEWAL_CHARGE_CONTRACT,
        parentContractId=LIVE_PARENT_CONTRACT,
        clientUtm={"utm_content": "payment_61"},
        amount=1500.0,
        currency="RUB",
        status="subscription-active",
    )
    event = LavaProvider.parse_webhook(raw)
    local_sub = make_lava_subscription(
        id=7,
        user_id=9,
        plan_id=1,
        provider_subscription_id=LIVE_PARENT_CONTRACT,
    )
    record = SimpleNamespace(payment_id=None)
    fulfilled = []
    alerts = []
    fulfilled_sub = SimpleNamespace(id=7, invite_link=None)

    async def fake_fulfill(_session, _bot, paid):
        fulfilled.append(paid)
        paid.approved_at = datetime(2026, 8, 5, tzinfo=UTC)
        return fulfilled_sub

    async def fake_notify(_session, _payment, _subscription):
        return True

    async def fake_alert(text, **kwargs):  # pragma: no cover - must stay empty
        alerts.append((text, kwargs))
        return True

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(webhooks_in, "notify_payment_succeeded", fake_notify)
    monkeypatch.setattr(webhooks_in, "send_ops_alert", fake_alert)

    # Queued in call order: charge-id lookup, parent-contract pending lookup,
    # then the subscription lookup that supplies user/plan.
    db = FakeDB(None, None, local_sub)
    result = await webhooks_in._handle_lava_invoice_paid(db, event, record)

    assert result == {"ok": True, "fulfilled": True}
    assert len(fulfilled) == 1
    renewal = fulfilled[0]
    # A genuinely new row, carried by the subscription's identity — not payment #61.
    assert renewal in db.added
    assert renewal.id != 61
    assert renewal.is_renewal is True
    assert renewal.user_id == 9
    assert renewal.plan_id == 1
    assert renewal.status == "succeeded"
    assert renewal.amount == Decimal("1500.0")
    assert renewal.currency == "RUB"
    # The charge is identified by its own contract id; the chain stays on the parent.
    assert renewal.lava_invoice_id == RENEWAL_CHARGE_CONTRACT
    assert renewal.lava_subscription_id == LIVE_PARENT_CONTRACT
    assert renewal.provider_event_id == (
        f"subscription.recurring.payment.success:{RENEWAL_CHARGE_CONTRACT}"
    )
    assert record.payment_id == renewal.id
    # A renewal that lands correctly is not an incident (GK-416).
    assert alerts == []


@pytest.mark.asyncio
async def test_lava_renewal_never_resolves_payment_via_client_utm(lava_settings):
    """A renewal must never be correlated by clientUtm.

    ``utm_content=payment_{id}`` identifies the *checkout*, so on a recurring
    charge it points at the first purchase — a payment that is already fulfilled.
    Consulting it would reintroduce the silent no-op above, which is why
    ``_find_lava_payment_for_invoice`` skips that lookup for renewal events.

    Enforced structurally: only two queries are queued. If the clientUtm branch
    ever runs again it consumes one of them and the parent-contract lookup then
    hits an empty queue, failing this test loudly.
    """
    raw = lava_payload(
        eventType="subscription.recurring.payment.success",
        contractId=RENEWAL_CHARGE_CONTRACT,
        parentContractId=LIVE_PARENT_CONTRACT,
        clientUtm={"utm_content": "payment_61"},
    )
    event = LavaProvider.parse_webhook(raw)
    # The UTM is parsed and present — the guard, not a missing value, is what
    # keeps it from being used.
    assert event.metadata["payment_id"] == "61"
    assert event.metadata["lava_is_renewal"] == "true"

    db = FakeDB(None, None)
    found = await webhooks_in._find_lava_payment_for_invoice(db, event.metadata)

    assert found is None
    assert db.values == []
