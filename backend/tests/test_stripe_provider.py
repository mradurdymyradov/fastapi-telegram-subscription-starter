from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routers import webhooks_in
from app.db.models import Payment
from app.payments import stripe_provider
from app.payments.base import PaymentEvent
from app.payments.stripe_provider import StripeProvider
from app.services import subscription as subscription_service
from app.services.subscription import create_or_extend_subscription

#: GK-439 launch window: paid access starts 01.09, payments open ~19.08.
GK439_FLOOR = datetime(2026, 9, 1, tzinfo=UTC)
GK439_AUGUST_PAYMENT = datetime(2026, 8, 19, 14, 30, tzinfo=UTC)


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _NoopNested:
    """Async context manager standing in for ``session.begin_nested()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, prior_payment_id=None):
        self.prior_payment_id = prior_payment_id
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def expunge(self, obj):
        if obj in self.added:
            self.added.remove(obj)

    def begin_nested(self):
        return _NoopNested()

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 123

    async def execute(self, _query):
        return ScalarResult(self.prior_payment_id)


class FakeDB:
    def __init__(self, *values):
        self.values = list(values)
        self.added = []
        self.flushed = 0
        self.rolled_back = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1
        for i, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = 900 + i

    async def execute(self, _query):
        return ScalarResult(self.values.pop(0))

    async def rollback(self):
        self.rolled_back = True


class FakeRequest:
    _body = b"{}"

    @property
    def headers(self):
        return {"content-length": str(len(self._body))}

    async def stream(self):
        yield self._body

    async def body(self):
        return self._body


def make_user(**overrides):
    data = {
        "id": 10,
        "tg_id": 10010,
        "username": "member",
        "first_name": "Member",
        "last_name": "One",
        "referrer_id": 99,
        "stripe_customer_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_plan(**overrides):
    data = {
        "id": 20,
        "code": "1m",
        "name": "1 month",
        "description": "Monthly access",
        "price_usd": Decimal("19.00"),
        "duration_days": 30,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture
def stripe_settings(monkeypatch):
    settings = SimpleNamespace(
        stripe_secret_key="sk_test_123",
        stripe_webhook_secret="whsec_123",
        stripe_referral_coupon_id="coupon_referral_20",
        stripe_price_monthly_id="price_monthly",
        stripe_price_6m_id="price_6m",
        stripe_price_annual_id="price_annual",
        public_base_url="https://example.test",
        bot_username="membership_saas_test_bot",
        bot_token="",
    )
    monkeypatch.setattr(stripe_provider, "settings", settings)
    monkeypatch.setattr(webhooks_in, "settings", settings)
    return settings


@pytest.fixture(autouse=True)
def stripe_billing_notifications_noop(monkeypatch):
    async def noop(*_args, **_kwargs):
        return True

    monkeypatch.setattr(webhooks_in, "notify_payment_succeeded", noop)
    monkeypatch.setattr(webhooks_in, "notify_payment_failed", noop)
    monkeypatch.setattr(webhooks_in, "notify_subscription_cancelled", noop)


#: What the three launch Prices are configured to charge at Stripe. The
#: price-drift guard (GK-421/GK-422) reads these, so tests that exercise
#: checkout have to describe them — that is the point of the guard.
LAUNCH_PRICES = {
    "price_monthly": {"unit_amount": 1900, "currency": "usd", "interval": "month", "interval_count": 1},
    "price_6m": {"unit_amount": 8900, "currency": "usd", "interval": "month", "interval_count": 6},
    "price_annual": {"unit_amount": 14900, "currency": "usd", "interval": "year", "interval_count": 1},
}


def price_object(**overrides):
    data = {"unit_amount": 1900, "currency": "usd", "interval": "month", "interval_count": 1}
    data.update(overrides)
    return {
        "id": data.get("id", "price_monthly"),
        "unit_amount": data["unit_amount"],
        "currency": data["currency"],
        "active": data.get("active", True),
        "recurring": {
            "interval": data["interval"],
            "interval_count": data["interval_count"],
        },
    }


@pytest.fixture(autouse=True)
def forget_validated_prices():
    """The guard caches exact price/plan comparisons; tests must not inherit it."""
    stripe_provider._VALIDATED_PRICE_EXPECTATIONS.clear()
    yield
    stripe_provider._VALIDATED_PRICE_EXPECTATIONS.clear()


@pytest.fixture(autouse=True)
def no_access_start_floor(monkeypatch):
    """GK-439: while the launch floor is set the checkout shape changes (a
    prepaid one-time line item plus a trial). Every test in this file describes
    the ordinary shape, so pin the floor off explicitly rather than depending on
    whichever `.env` the test container happened to be built with."""
    monkeypatch.setattr(
        subscription_service, "settings", SimpleNamespace(access_start_floor_at=None)
    )


@pytest.fixture
def access_start_floor(monkeypatch):
    """Turn the GK-439 launch floor on, at a fixed instant, with a frozen clock."""

    def _apply(floor: datetime = GK439_FLOOR, *, now: datetime = GK439_AUGUST_PAYMENT):
        monkeypatch.setattr(
            subscription_service, "settings", SimpleNamespace(access_start_floor_at=floor)
        )
        monkeypatch.setattr(subscription_service, "utcnow", lambda: now)
        return floor

    return _apply


@pytest.fixture
def stripe_api(monkeypatch):
    calls = SimpleNamespace(customers=[], sessions=[], prices=[])

    def fake_customer_create(**kwargs):
        calls.customers.append(kwargs)
        return SimpleNamespace(id="cus_123")

    def fake_session_create(**kwargs):
        calls.sessions.append(kwargs)
        return SimpleNamespace(id="cs_123", url="https://checkout.stripe.test/cs_123")

    def fake_price_retrieve(price_id, **_kwargs):
        calls.prices.append(price_id)
        if price_id not in LAUNCH_PRICES:
            raise AssertionError(f"unexpected Stripe price retrieve: {price_id}")
        return price_object(id=price_id, **LAUNCH_PRICES[price_id])

    monkeypatch.setattr(stripe_provider.stripe.Customer, "create", fake_customer_create)
    monkeypatch.setattr(stripe_provider.stripe.checkout.Session, "create", fake_session_create)
    monkeypatch.setattr(stripe_provider.stripe.Price, "retrieve", fake_price_retrieve)
    monkeypatch.setattr(stripe_provider, "_ensure_referral_coupon", lambda: "coupon_referral_20")
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "expected_price", "expected_discount", "expected_amount"),
    [
        (make_plan(code="1m", duration_days=30, price_usd=Decimal("19.00")), "price_monthly", True, Decimal("15.20")),
        (make_plan(code="6m", duration_days=180, price_usd=Decimal("89.00")), "price_6m", False, Decimal("89.00")),
        (make_plan(code="12m", duration_days=365, price_usd=Decimal("149.00")), "price_annual", False, Decimal("149.00")),
    ],
)
async def test_create_checkout_uses_precreated_price_ids_for_launch_plans(
    stripe_settings,
    stripe_api,
    plan,
    expected_price,
    expected_discount,
    expected_amount,
):
    user = make_user()
    session = FakeSession(prior_payment_id=None)

    result = await StripeProvider.create_checkout(session, user, plan)

    assert result.payment_id == 123
    assert result.url == "https://checkout.stripe.test/cs_123"
    assert user.stripe_customer_id == "cus_123"
    # A referral reservation (GK-402) is added before the Payment on the
    # discounted monthly path, so locate the Payment by type rather than index.
    payment_row = next(o for o in session.added if isinstance(o, Payment))
    assert payment_row.amount == expected_amount
    assert payment_row.stripe_checkout_session_id == "cs_123"
    assert payment_row.external_id == "cs_123"

    customer_payload = stripe_api.customers[0]
    assert customer_payload["metadata"] == {"user_id": "10", "tg_id": "10010"}
    assert customer_payload["idempotency_key"] == "membership_saas-stripe-customer-user-10"

    checkout_payload = stripe_api.sessions[0]
    assert checkout_payload["mode"] == "subscription"
    assert checkout_payload["line_items"] == [{"price": expected_price, "quantity": 1}]
    assert checkout_payload["idempotency_key"] == "membership_saas-stripe-checkout-payment-123"
    assert checkout_payload["customer"] == "cus_123"
    assert checkout_payload["client_reference_id"] == "123"
    assert checkout_payload["metadata"]["payment_id"] == "123"
    assert checkout_payload["subscription_data"]["metadata"]["payment_id"] == "123"
    if expected_discount:
        assert checkout_payload["discounts"] == [{"coupon": "coupon_referral_20"}]
        assert checkout_payload["metadata"]["referral_discount"] == "monthly_first_invoice_20"
    else:
        assert "discounts" not in checkout_payload
        assert "referral_discount" not in checkout_payload["metadata"]


@pytest.mark.asyncio
async def test_create_checkout_suppresses_monthly_referral_discount_after_first_purchase(
    stripe_settings,
    stripe_api,
):
    user = make_user()
    session = FakeSession(prior_payment_id=777)

    await StripeProvider.create_checkout(session, user, make_plan())

    assert session.added[0].amount == Decimal("19.00")
    assert "discounts" not in stripe_api.sessions[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "expected_cents", "expected_term"),
    [
        (make_plan(code="1m", name="1 month", duration_days=30, price_usd=Decimal("19.00")), 1900, "1m"),
        (make_plan(code="6m", name="6 months", duration_days=180, price_usd=Decimal("89.00")), 8900, "6m"),
        (make_plan(code="12m", name="12 months", duration_days=365, price_usd=Decimal("149.00")), 14900, "12m"),
    ],
)
async def test_gift_checkout_is_one_time_full_list_price_for_all_launch_terms(
    stripe_settings,
    stripe_api,
    plan,
    expected_cents,
    expected_term,
):
    user = make_user(referrer_id=99)
    session = FakeSession(prior_payment_id=None)

    result = await StripeProvider.create_checkout(
        session,
        user,
        plan,
        is_gift=True,
        promo_code="WELCOME20",
    )

    assert result.payment_id == 123
    payment = session.added[0]
    assert payment.is_gift is True
    assert payment.amount == plan.price_usd

    checkout = stripe_api.sessions[0]
    assert checkout["mode"] == "payment"
    assert "subscription_data" not in checkout
    assert "discounts" not in checkout
    assert checkout["line_items"] == [
        {
            "price_data": {
                "currency": "usd",
                "unit_amount": expected_cents,
                "product_data": {
                    "name": f"Подарочная подписка: {plan.name}",
                    "metadata": {"plan_id": "20", "gift_term": expected_term},
                },
            },
            "quantity": 1,
        }
    ]
    metadata = checkout["metadata"]
    assert metadata["is_gift"] == "true"
    assert metadata["gift_term"] == expected_term
    assert metadata["gift_duration_days"] == str(plan.duration_days)
    assert metadata["gift_list_price"] == f"{plan.price_usd:.2f}"
    assert metadata["gift_currency"] == "USD"
    assert "promo_code" not in metadata
    assert "referral_discount" not in metadata
    assert checkout["payment_intent_data"] == {"metadata": metadata}


def test_price_id_config_rejects_product_ids(stripe_settings):
    stripe_settings.stripe_price_monthly_id = "prod_not_a_price"

    with pytest.raises(ValueError, match="must start with 'price_'"):
        stripe_provider._price_id_for_plan(
            stripe_provider.StripeRecurring(interval="month", interval_count=1, launch_code="1m")
        )


# ---------------------------------------------------------------------------
# GK-421 / GK-422: the price-drift guard
#
# GK-422 was a real, live defect: the bot advertised $79 / $129 for 6m / 12m
# while the configured Stripe Prices still charged $89 / $149. Nothing in the
# code compared the two, so the only thing standing between a member and a
# wrong charge was somebody remembering to update both places. These tests are
# that comparison.
# ---------------------------------------------------------------------------

MONTHLY = stripe_provider.StripeRecurring(interval="month", interval_count=1, launch_code="1m")


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        # The literal GK-422 shape: Stripe charges more than the bot displays.
        ({"unit_amount": 8900}, "charges 89.00 but the plan shows 19.00"),
        # ...and the mirror image, which loses money instead of trust.
        ({"unit_amount": 900}, "charges 9.00 but the plan shows 19.00"),
        ({"currency": "eur"}, "currency is eur, expected usd"),
        # A yearly Price behind the monthly button bills 12× what was shown.
        ({"interval": "year", "interval_count": 1}, "bills every 1 year, expected every 1 month"),
        ({"interval_count": 6}, "bills every 6 month, expected every 1 month"),
        ({"active": False}, "archived"),
        # Tiered/metered prices have no unit_amount to compare at all.
        ({"unit_amount": None}, "no unit_amount"),
    ],
)
def test_price_drift_is_refused_rather_than_charged(monkeypatch, overrides, expected_message):
    monkeypatch.setattr(
        stripe_provider.stripe.Price,
        "retrieve",
        lambda _pid, **_kw: price_object(**overrides),
    )

    with pytest.raises(stripe_provider.StripePriceMismatch, match=expected_message):
        stripe_provider._validate_price_matches_plan(
            "price_monthly", expected_amount=Decimal("19.00"), recurring=MONTHLY
        )


def test_price_that_cannot_be_verified_is_refused_not_assumed_good(monkeypatch):
    """Fail closed: an unreachable Stripe is not evidence the price is right."""

    def unreachable(_pid, **_kw):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(stripe_provider.stripe.Price, "retrieve", unreachable)

    with pytest.raises(stripe_provider.StripePriceMismatch, match="could not verify"):
        stripe_provider._validate_price_matches_plan(
            "price_monthly", expected_amount=Decimal("19.00"), recurring=MONTHLY
        )


def test_matching_price_and_plan_expectations_are_checked_only_once(monkeypatch):
    """An unchanged Stripe price / local plan comparison is safe to cache."""
    calls = []

    def counted(price_id, **_kw):
        calls.append(price_id)
        return price_object()

    monkeypatch.setattr(stripe_provider.stripe.Price, "retrieve", counted)

    for _ in range(3):
        stripe_provider._validate_price_matches_plan(
            "price_monthly", expected_amount=Decimal("19.00"), recurring=MONTHLY
        )

    assert calls == ["price_monthly"]


@pytest.mark.parametrize(
    ("changed_amount", "changed_recurring", "expected_message"),
    [
        (
            Decimal("20.00"),
            MONTHLY,
            "charges 19.00 but the plan shows 20.00",
        ),
        (
            Decimal("19.00"),
            stripe_provider.StripeRecurring(
                interval="month", interval_count=6, launch_code="6m"
            ),
            "bills every 1 month, expected every 6 month",
        ),
        (
            Decimal("19.00"),
            stripe_provider.StripeRecurring(
                interval="year", interval_count=1, launch_code="12m"
            ),
            "bills every 1 month, expected every 1 year",
        ),
    ],
)
def test_changed_plan_expectations_force_revalidation(
    monkeypatch, changed_amount, changed_recurring, expected_message
):
    """GK-456: repricing a Plan must not inherit the old process cache hit."""
    calls = []

    def counted(price_id, **_kw):
        calls.append(price_id)
        return price_object()

    monkeypatch.setattr(stripe_provider.stripe.Price, "retrieve", counted)

    stripe_provider._validate_price_matches_plan(
        "price_monthly", expected_amount=Decimal("19.00"), recurring=MONTHLY
    )

    with pytest.raises(stripe_provider.StripePriceMismatch, match=expected_message):
        stripe_provider._validate_price_matches_plan(
            "price_monthly",
            expected_amount=changed_amount,
            recurring=changed_recurring,
        )

    assert calls == ["price_monthly", "price_monthly"]


@pytest.mark.asyncio
async def test_checkout_refuses_when_the_configured_price_charges_the_wrong_amount(
    stripe_settings, stripe_api, monkeypatch
):
    """End-to-end: no Checkout Session is created for a drifted price."""
    monkeypatch.setattr(
        stripe_provider.stripe.Price,
        "retrieve",
        lambda _pid, **_kw: price_object(unit_amount=8900),
    )

    with pytest.raises(stripe_provider.StripePriceMismatch):
        await StripeProvider.create_checkout(FakeSession(), make_user(), make_plan())

    assert stripe_api.sessions == [], "a mispriced checkout must never reach Stripe"


@pytest.mark.asyncio
async def test_gift_checkout_needs_no_price_lookup(stripe_settings, stripe_api):
    """Gifts build their own inline price_data, so there is nothing to drift."""
    await StripeProvider.create_checkout(
        FakeSession(), make_user(), make_plan(), is_gift=True
    )

    assert stripe_api.prices == []


# ---------------------------------------------------------------------------
# GK-421: a stale stripe_customer_id must self-heal
#
# On 28.07 the stored customer had been deleted in the Stripe dashboard;
# `No such customer: cus_UfnIz3B6Xr9bOn` came back six times in two minutes and
# was fixed by hand by nulling the column. Nothing stopped it recurring.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_customer_is_reused(monkeypatch, stripe_settings, stripe_api):
    monkeypatch.setattr(
        stripe_provider.stripe.Customer,
        "retrieve",
        lambda _cid, **_kw: {"id": "cus_live", "deleted": False},
    )

    customer_id = await stripe_provider._ensure_customer(make_user(stripe_customer_id="cus_live"))

    assert customer_id == "cus_live"
    assert stripe_api.customers == [], "an existing customer must not be recreated"


@pytest.mark.parametrize(
    "retrieve",
    [
        # Deleted in the dashboard: Stripe answers with deleted=true.
        pytest.param(lambda _cid, **_kw: {"id": "cus_dead", "deleted": True}, id="deleted-flag"),
        # Deleted long enough ago / wrong account: the retrieve itself raises.
        pytest.param(
            lambda _cid, **_kw: (_ for _ in ()).throw(RuntimeError("No such customer: cus_dead")),
            id="retrieve-raises",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stale_customer_is_recreated_with_a_fresh_idempotency_key(
    monkeypatch, stripe_settings, stripe_api, retrieve
):
    monkeypatch.setattr(stripe_provider.stripe.Customer, "retrieve", retrieve)

    customer_id = await stripe_provider._ensure_customer(make_user(stripe_customer_id="cus_dead"))

    assert customer_id == "cus_123"
    # Reusing the original key would make Stripe replay the 24h-cached response
    # and hand back the very customer we just rejected — the loop stays broken.
    assert (
        stripe_api.customers[0]["idempotency_key"]
        == "membership_saas-stripe-customer-user-10-after-cus_dead"
    )


def test_parse_checkout_completed_webhook_binds_ids_without_success(monkeypatch, stripe_settings):
    def fake_construct_event(_payload, _signature, _secret):
        return {
            "id": "evt_123",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_123",
                    "customer": "cus_123",
                    "subscription": "sub_123",
                    "payment_intent": "pi_123",
                    "payment_status": "paid",
                    "amount_total": 1900,
                    "currency": "usd",
                    "metadata": {"payment_id": "123"},
                }
            },
        }

    monkeypatch.setattr(stripe_provider.stripe.Webhook, "construct_event", fake_construct_event)

    event = StripeProvider.parse_webhook(b"{}", "sig")

    assert event == PaymentEvent(
        external_id="cs_123",
        status="checkout_completed",
        amount=19,
        currency="USD",
        metadata={
            "payment_id": "123",
            "stripe_event_id": "evt_123",
            "stripe_event_type": "checkout.session.completed",
            "stripe_checkout_session_id": "cs_123",
            "stripe_customer_id": "cus_123",
            "stripe_subscription_id": "sub_123",
            "stripe_payment_intent_id": "pi_123",
            "stripe_payment_status": "paid",
        },
    )


@pytest.mark.asyncio
async def test_stripe_webhook_missing_signature_returns_400(stripe_settings):
    with pytest.raises(HTTPException) as exc_info:
        await webhooks_in.stripe_webhook(FakeRequest(), FakeDB(), None)

    assert exc_info.value.status_code == 400
    assert "Missing Stripe-Signature" in exc_info.value.detail


@pytest.mark.asyncio
async def test_stripe_webhook_bad_signature_returns_400(monkeypatch, stripe_settings):
    def fake_construct_event(_payload, _signature, _secret):
        raise ValueError("bad signature")

    monkeypatch.setattr(stripe_provider.stripe.Webhook, "construct_event", fake_construct_event)

    with pytest.raises(HTTPException) as exc_info:
        await webhooks_in.stripe_webhook(FakeRequest(), FakeDB(), "sig")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid Stripe webhook signature"


@pytest.mark.asyncio
async def test_stripe_webhook_unsupported_signed_event_still_ignored(
    monkeypatch,
    stripe_settings,
):
    def fake_construct_event(_payload, _signature, _secret):
        return {
            "id": "evt_customer",
            "type": "customer.created",
            "data": {"object": {"id": "cus_123"}},
        }

    monkeypatch.setattr(stripe_provider.stripe.Webhook, "construct_event", fake_construct_event)

    result = await webhooks_in.stripe_webhook(FakeRequest(), FakeDB(), "sig")

    assert result == {"ok": True, "ignored": True}


@pytest.mark.asyncio
async def test_stripe_webhook_checkout_completed_does_not_fulfill(monkeypatch, stripe_settings):
    event = PaymentEvent(
        external_id="cs_123",
        status="checkout_completed",
        amount=19,
        currency="USD",
        metadata={
            "payment_id": "123",
            "stripe_checkout_session_id": "cs_123",
            "stripe_customer_id": "cus_123",
            "stripe_subscription_id": "sub_123",
        },
    )
    payment = SimpleNamespace(
        id=123,
        user_id=10,
        status="pending",
        approved_at=None,
        stripe_checkout_session_id=None,
        external_id=None,
    )
    user = SimpleNamespace(id=10, stripe_customer_id=None)

    async def fail_fulfill(*_args, **_kwargs):
        raise AssertionError("checkout.session.completed must not provision access")

    monkeypatch.setattr(webhooks_in.StripeProvider, "parse_webhook", lambda _body, _sig: event)
    monkeypatch.setattr(webhooks_in, "fulfill_payment", fail_fulfill)

    result = await webhooks_in.stripe_webhook(FakeRequest(), FakeDB(payment, user), None)

    assert result == {"ok": True, "bound": True}
    assert payment.stripe_checkout_session_id == "cs_123"
    assert payment.external_id == "sub_123"
    assert user.stripe_customer_id == "cus_123"


@pytest.mark.asyncio
async def test_paid_one_time_gift_checkout_fulfills_activation_once(monkeypatch, stripe_settings):
    event = PaymentEvent(
        external_id="cs_gift",
        status="checkout_completed",
        amount=89,
        currency="USD",
        metadata={
            "payment_id": "123",
            "stripe_event_id": "evt_gift_checkout",
            "stripe_checkout_session_id": "cs_gift",
            "stripe_customer_id": "cus_123",
            "stripe_subscription_id": "",
            "stripe_payment_intent_id": "pi_gift",
            "stripe_payment_status": "paid",
        },
    )
    payment = SimpleNamespace(
        id=123,
        user_id=10,
        is_gift=True,
        status="pending",
        amount=Decimal("89.00"),
        currency="USD",
        approved_at=None,
        stripe_checkout_session_id=None,
        stripe_payment_intent_id=None,
        provider_event_id=None,
        external_id="cs_gift",
    )
    user = SimpleNamespace(id=10, stripe_customer_id=None)
    record = SimpleNamespace(payment_id=None)
    fulfilled = []
    notified = []

    async def fake_fulfill(session, bot, paid):
        fulfilled.append((session, bot, paid))
        paid.approved_at = datetime(2026, 6, 21, tzinfo=UTC)
        return None

    async def fake_notify(session, paid, subscription):
        notified.append((session, paid, subscription))
        return True

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(webhooks_in, "notify_payment_succeeded", fake_notify)
    db = FakeDB(user)

    result = await webhooks_in._handle_stripe_checkout_completed(db, event, record, payment)

    assert result == {"ok": True, "fulfilled": True}
    assert record.payment_id == payment.id
    assert payment.status == "succeeded"
    assert payment.amount == Decimal("89")
    assert payment.provider_event_id == "evt_gift_checkout"
    assert payment.stripe_checkout_session_id == "cs_gift"
    assert payment.stripe_payment_intent_id == "pi_gift"
    assert user.stripe_customer_id == "cus_123"
    assert fulfilled == [(db, None, payment)]
    assert notified == [(db, payment, None)]


@pytest.mark.asyncio
async def test_stripe_duplicate_event_id_returns_already_without_side_effects(
    monkeypatch,
    stripe_settings,
):
    event = PaymentEvent(
        external_id="in_123",
        status="invoice_paid",
        amount=19,
        currency="USD",
        metadata={
            "payment_id": "123",
            "stripe_event_id": "evt_replayed",
            "stripe_event_type": "invoice.paid",
            "stripe_invoice_id": "in_123",
            "stripe_subscription_id": "sub_123",
        },
    )

    async def duplicate_record(_db, _event, *, raw_hash):
        assert raw_hash == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        return None

    async def fail_fulfill(*_args, **_kwargs):
        raise AssertionError("duplicate Stripe events must not re-run fulfillment")

    monkeypatch.setattr(webhooks_in.StripeProvider, "parse_webhook", lambda _body, _sig: event)
    monkeypatch.setattr(webhooks_in, "_record_stripe_event_once", duplicate_record)
    monkeypatch.setattr(webhooks_in, "fulfill_payment", fail_fulfill)

    result = await webhooks_in.stripe_webhook(FakeRequest(), FakeDB(), "sig")

    assert result == {"ok": True, "already": True}


@pytest.mark.asyncio
async def test_find_stripe_payment_for_invoice_scopes_invoice_id_to_stripe_provider():
    seen_sql = []

    class InspectDB:
        async def execute(self, query):
            seen_sql.append(str(query.compile(compile_kwargs={"literal_binds": True})))
            return ScalarResult(None)

    await webhooks_in._find_stripe_payment_for_invoice(
        InspectDB(),
        {"stripe_invoice_id": "in_123"},
    )

    assert "payments.provider = 'stripe'" in seen_sql[0]
    assert "payments.stripe_invoice_id = 'in_123'" in seen_sql[0]


def test_parse_invoice_paid_webhook_extracts_invoice_lifecycle(monkeypatch, stripe_settings):
    def fake_construct_event(_payload, _signature, _secret):
        return {
            "id": "evt_invoice_paid",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_123",
                    "customer": "cus_123",
                    "subscription": "sub_123",
                    "payment_intent": "pi_123",
                    "amount_paid": 1520,
                    "currency": "usd",
                    "status": "paid",
                    "billing_reason": "subscription_create",
                    "metadata": {},
                    "parent": {
                        "subscription_details": {
                            "metadata": {
                                "payment_id": "123",
                                "user_id": "10",
                                "plan_id": "20",
                            }
                        }
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {
                                    "start": 1_778_198_400,
                                    "end": 1_780_790_400,
                                }
                            }
                        ]
                    },
                }
            },
        }

    monkeypatch.setattr(stripe_provider.stripe.Webhook, "construct_event", fake_construct_event)

    event = StripeProvider.parse_webhook(b"{}", "sig")

    assert event.status == "invoice_paid"
    assert event.external_id == "in_123"
    assert event.amount == 15.2
    assert event.currency == "USD"
    assert event.metadata["payment_id"] == "123"
    assert event.metadata["stripe_event_id"] == "evt_invoice_paid"
    assert event.metadata["stripe_event_type"] == "invoice.paid"
    assert event.metadata["stripe_invoice_id"] == "in_123"
    assert event.metadata["stripe_subscription_id"] == "sub_123"
    assert event.metadata["stripe_invoice_period_start"] == "1778198400"
    assert event.metadata["stripe_invoice_period_end"] == "1780790400"


@pytest.mark.asyncio
async def test_stripe_invoice_sequence_fulfills_only_once(monkeypatch, stripe_settings):
    checkout_event = PaymentEvent(
        external_id="cs_123",
        status="checkout_completed",
        amount=19,
        currency="USD",
        metadata={
            "payment_id": "123",
            "stripe_event_id": "evt_checkout",
            "stripe_event_type": "checkout.session.completed",
            "stripe_checkout_session_id": "cs_123",
            "stripe_customer_id": "cus_123",
            "stripe_subscription_id": "sub_123",
        },
    )
    invoice_event = PaymentEvent(
        external_id="in_123",
        status="invoice_paid",
        amount=15.2,
        currency="USD",
        metadata={
            "payment_id": "123",
            "user_id": "10",
            "plan_id": "20",
            "stripe_event_id": "evt_invoice_paid_1",
            "stripe_event_type": "invoice.paid",
            "stripe_invoice_id": "in_123",
            "stripe_subscription_id": "sub_123",
            "stripe_invoice_billing_reason": "subscription_create",
            "stripe_invoice_period_start": "1778198400",
            "stripe_invoice_period_end": "1780790400",
        },
    )
    duplicate_invoice_event = PaymentEvent(
        external_id="in_123",
        status="invoice_paid",
        amount=15.2,
        currency="USD",
        metadata={
            **invoice_event.metadata,
            "stripe_event_id": "evt_invoice_paid_2",
        },
    )
    payment = SimpleNamespace(
        id=123,
        user_id=10,
        plan_id=20,
        provider="stripe",
        amount=Decimal("19.00"),
        currency="USD",
        status="pending",
        approved_at=None,
        stripe_checkout_session_id=None,
        stripe_invoice_id=None,
        provider_event_id=None,
        external_id=None,
        is_renewal=False,
        is_gift=False,
        gift_recipient_id=None,
        billing_period_start=None,
        billing_period_end=None,
    )
    user = SimpleNamespace(id=10, stripe_customer_id=None)
    calls = SimpleNamespace(subscription_extensions=0, referral_commissions=0)

    async def fake_fulfill(_session, _bot, paid):
        calls.subscription_extensions += 1
        if not paid.is_renewal:
            calls.referral_commissions += 1
        paid.approved_at = datetime(2026, 5, 30, tzinfo=UTC)
        return SimpleNamespace(id=50, invite_link="invite-link")

    events = [checkout_event, invoice_event, duplicate_invoice_event]
    monkeypatch.setattr(
        webhooks_in.StripeProvider,
        "parse_webhook",
        lambda _body, _sig: events.pop(0),
    )
    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)
    db = FakeDB(payment, user, None, payment, payment)

    checkout_result = await webhooks_in.stripe_webhook(FakeRequest(), db, "sig")
    invoice_result = await webhooks_in.stripe_webhook(FakeRequest(), db, "sig")
    duplicate_result = await webhooks_in.stripe_webhook(FakeRequest(), db, "sig")

    assert checkout_result == {"ok": True, "bound": True}
    assert invoice_result == {"ok": True, "fulfilled": True}
    assert duplicate_result == {"ok": True, "already": True}
    assert payment.stripe_checkout_session_id == "cs_123"
    assert payment.external_id == "sub_123"
    assert payment.stripe_invoice_id == "in_123"
    assert payment.provider_event_id == "evt_invoice_paid_1"
    assert payment.amount == Decimal("15.2")
    assert payment.billing_period_start == datetime(2026, 5, 8, tzinfo=UTC)
    assert payment.billing_period_end == datetime(2026, 6, 7, tzinfo=UTC)
    assert calls.subscription_extensions == 1
    assert calls.referral_commissions == 1


@pytest.mark.asyncio
async def test_stripe_invoice_payment_failed_updates_payment_and_subscription(
    monkeypatch,
    stripe_settings,
):
    event = PaymentEvent(
        external_id="in_failed",
        status="invoice_payment_failed",
        amount=19,
        currency="USD",
        metadata={
            "payment_id": "123",
            "stripe_event_id": "evt_failed",
            "stripe_invoice_id": "in_failed",
            "stripe_subscription_id": "sub_123",
            "stripe_invoice_period_start": "1778198400",
            "stripe_invoice_period_end": "1780790400",
        },
    )
    payment = SimpleNamespace(
        id=123,
        provider="stripe",
        status="pending",
        approved_at=None,
        amount=Decimal("19.00"),
        currency="USD",
        provider_event_id=None,
        stripe_invoice_id=None,
        external_id=None,
        is_renewal=False,
        billing_period_start=None,
        billing_period_end=None,
    )
    sub = SimpleNamespace(provider_status="active")
    record = SimpleNamespace(payment_id=None)
    notifications = []

    async def fake_notify(session, *, payment, subscription, provider):
        notifications.append((session, payment, subscription, provider))
        return True

    monkeypatch.setattr(webhooks_in, "notify_payment_failed", fake_notify)

    db = FakeDB(payment, sub)
    result = await webhooks_in._handle_stripe_invoice_payment_failed(
        db,
        event,
        record,
    )

    assert result == {"ok": True, "updated": True}
    assert record.payment_id == 123
    assert payment.status == "failed"
    assert payment.stripe_invoice_id == "in_failed"
    assert sub.provider_status == "past_due"
    assert notifications == [(db, payment, sub, "stripe")]


@pytest.mark.asyncio
async def test_stripe_renewal_invoice_ignores_initial_checkout_payment_id(
    monkeypatch,
    stripe_settings,
):
    event = PaymentEvent(
        external_id="in_renewal",
        status="invoice_paid",
        amount=19,
        currency="USD",
        metadata={
            # Stripe subscription metadata can keep the initial checkout payment_id.
            # Renewal invoices must create their own local Payment instead.
            "payment_id": "123",
            "stripe_event_id": "evt_renewal",
            "stripe_event_type": "invoice.paid",
            "stripe_invoice_id": "in_renewal",
            "stripe_subscription_id": "sub_123",
            "stripe_invoice_billing_reason": "subscription_cycle",
            "stripe_invoice_period_start": "1780790400",
            "stripe_invoice_period_end": "1783468800",
        },
    )
    local_sub = SimpleNamespace(user_id=10, plan_id=20)
    record = SimpleNamespace(payment_id=None)
    calls = []
    notified = []
    fulfilled_sub = SimpleNamespace(id=51, invite_link=None)

    async def fake_fulfill(_session, _bot, paid):
        calls.append(paid)
        paid.approved_at = datetime(2026, 6, 7, tzinfo=UTC)
        return fulfilled_sub

    async def fake_notify(session, payment, subscription):
        notified.append((session, payment, subscription))
        return True

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(webhooks_in, "notify_payment_succeeded", fake_notify)
    db = FakeDB(None, None, local_sub)

    result = await webhooks_in._handle_stripe_invoice_paid(db, event, record)

    assert result == {"ok": True, "fulfilled": True}
    assert len(calls) == 1
    renewal = calls[0]
    assert renewal in db.added
    assert renewal.id == record.payment_id
    assert renewal.user_id == 10
    assert renewal.plan_id == 20
    assert renewal.stripe_invoice_id == "in_renewal"
    assert renewal.external_id == "sub_123"
    assert renewal.is_renewal is True
    assert notified == [(db, renewal, fulfilled_sub)]


@pytest.mark.asyncio
async def test_stripe_invoice_without_local_payment_can_create_gift_fulfillment(
    monkeypatch,
    stripe_settings,
):
    event = PaymentEvent(
        external_id="in_gift",
        status="invoice_paid",
        amount=89,
        currency="USD",
        metadata={
            "user_id": "10",
            "plan_id": "20",
            "gift_recipient_id": "30",
            "stripe_event_id": "evt_gift_paid",
            "stripe_event_type": "invoice.paid",
            "stripe_invoice_id": "in_gift",
            "stripe_subscription_id": "sub_gift",
            "stripe_invoice_billing_reason": "subscription_create",
        },
    )
    record = SimpleNamespace(payment_id=None)
    fulfilled = []

    async def fake_fulfill(_session, _bot, paid):
        fulfilled.append(paid)
        paid.approved_at = datetime(2026, 6, 1, tzinfo=UTC)
        return SimpleNamespace(id=52, invite_link="gift-invite")

    monkeypatch.setattr(webhooks_in, "fulfill_payment", fake_fulfill)
    db = FakeDB(None, None, None)

    result = await webhooks_in._handle_stripe_invoice_paid(db, event, record)

    assert result == {"ok": True, "fulfilled": True}
    assert len(fulfilled) == 1
    gift_payment = fulfilled[0]
    assert gift_payment in db.added
    assert gift_payment.id == record.payment_id
    assert gift_payment.user_id == 10
    assert gift_payment.plan_id == 20
    assert gift_payment.is_gift is True
    assert gift_payment.gift_recipient_id == 30
    assert gift_payment.is_renewal is False
    assert gift_payment.stripe_invoice_id == "in_gift"
    assert gift_payment.external_id == "sub_gift"


@pytest.mark.asyncio
async def test_stripe_subscription_deleted_marks_local_subscription_cancelled(
    monkeypatch,
    stripe_settings,
):
    event = PaymentEvent(
        external_id="sub_123",
        status="subscription_deleted",
        amount=0,
        currency="USD",
        metadata={
            "stripe_event_id": "evt_sub_deleted",
            "stripe_subscription_id": "sub_123",
            "stripe_subscription_status": "canceled",
            "stripe_subscription_current_period_start": "1778198400",
            "stripe_subscription_current_period_end": "1780790400",
            "stripe_subscription_cancel_at_period_end": "false",
        },
    )
    sub = SimpleNamespace(
        id=50,
        user_id=10,
        plan_id=20,
        status="active",
        provider_status="active",
        current_period_start=None,
        current_period_end=None,
        cancel_at_period_end=True,
    )
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

    result = await webhooks_in._handle_stripe_subscription_event(db, event)

    assert result == {"ok": True, "updated": True}
    assert sub.status == "cancelled"
    assert sub.provider_status == "canceled"
    assert sub.current_period_start == datetime(2026, 5, 8, tzinfo=UTC)
    assert sub.current_period_end == datetime(2026, 6, 7, tzinfo=UTC)
    assert sub.cancel_at_period_end is False
    assert cancellations == [(db, 10, "stripe.subscription_cancelled")]
    assert notifications == [(db, sub, "stripe")]


# ---------------------------------------------------------------------------
# GK-439 option 1 — «деньги в августе, первое продление 1 октября»
#
# Our own access clock is provider-independent and covered in
# test_access_start_floor.py. What is covered here is the half that is NOT ours:
# Stripe starts billing from the checkout, so without this an August buyer has
# access to 01.10 and is charged again on 19.09 — early, every month, forever.
# ---------------------------------------------------------------------------


class _AccessSession:
    """Minimal session for `create_or_extend_subscription` — no prior access."""

    def __init__(self):
        self.added = []

    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "expected_price", "expected_cents"),
    [
        (make_plan(code="1m", name="1 month", duration_days=30, price_usd=Decimal("19.00")), "price_monthly", 1900),
        (make_plan(code="6m", name="6 months", duration_days=180, price_usd=Decimal("89.00")), "price_6m", 8900),
        (make_plan(code="12m", name="12 months", duration_days=365, price_usd=Decimal("149.00")), "price_annual", 14900),
    ],
)
async def test_checkout_prepays_the_first_period_and_defers_the_first_renewal(
    stripe_settings,
    stripe_api,
    access_start_floor,
    plan,
    expected_price,
    expected_cents,
):
    floor = access_start_floor()
    session = FakeSession(prior_payment_id=777)  # no referral discount in the way

    await StripeProvider.create_checkout(session, make_user(), plan)

    payload = stripe_api.sessions[0]
    assert payload["mode"] == "subscription"

    prepaid, recurring = payload["line_items"]
    assert prepaid["price_data"]["unit_amount"] == expected_cents
    assert prepaid["price_data"]["currency"] == "usd"
    assert recurring == {"price": expected_price, "quantity": 1}, (
        "the recurring Price must stay in the session — Stripe requires one in "
        "subscription mode, and it is what charges from the first renewal on"
    )

    first_renewal = floor + timedelta(days=plan.duration_days)
    assert payload["subscription_data"]["trial_end"] == int(first_renewal.timestamp())
    assert payload["metadata"]["first_renewal_at"] == first_renewal.isoformat()
    assert payload["metadata"]["access_start_floor"] == floor.isoformat()


@pytest.mark.asyncio
async def test_the_monthly_plan_lands_on_grants_first_of_october(
    stripe_settings, stripe_api, access_start_floor
):
    """Grant asked for one shared renewal date and named it. 01.09 + 30 days."""
    access_start_floor()
    session = FakeSession(prior_payment_id=777)

    await StripeProvider.create_checkout(session, make_user(), make_plan())

    trial_end = stripe_api.sessions[0]["subscription_data"]["trial_end"]
    assert datetime.fromtimestamp(trial_end, tz=UTC) == datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_stripes_next_charge_and_our_expires_at_are_the_same_instant(
    stripe_settings, stripe_api, access_start_floor
):
    """The acceptance criterion that spans both halves. Two clocks are set up
    here — the provider's, from the checkout payload, and ours, from the
    subscription the webhook would create — and they have to agree, or the
    member is charged on a day their access does not end."""
    access_start_floor()
    plan = make_plan()

    await StripeProvider.create_checkout(FakeSession(prior_payment_id=777), make_user(), plan)
    trial_end = stripe_api.sessions[0]["subscription_data"]["trial_end"]

    sub = await create_or_extend_subscription(
        _AccessSession(),
        user=SimpleNamespace(id=10, tg_id=10010),
        plan=plan,
        source="stripe",
        provider="stripe",
        # What the prepaid line item's invoice actually reports: the purchase
        # instant, not the access window.
        current_period_start=GK439_AUGUST_PAYMENT,
        current_period_end=GK439_AUGUST_PAYMENT,
    )

    assert datetime.fromtimestamp(trial_end, tz=UTC) == sub.expires_at


@pytest.mark.asyncio
async def test_with_the_floor_unset_checkout_keeps_todays_shape(stripe_settings, stripe_api):
    """`no_access_start_floor` is autouse, so this is the post-launch state:
    one recurring line item, no trial, no extra metadata."""
    session = FakeSession(prior_payment_id=777)

    await StripeProvider.create_checkout(session, make_user(), make_plan())

    payload = stripe_api.sessions[0]
    assert payload["line_items"] == [{"price": "price_monthly", "quantity": 1}]
    assert "trial_end" not in payload["subscription_data"]
    assert "first_renewal_at" not in payload["metadata"]


@pytest.mark.asyncio
async def test_a_gift_is_never_given_a_trial(stripe_settings, stripe_api, access_start_floor):
    """Gifts run in `mode: payment` — fixed-duration access, no recurring
    charge — so there is no renewal to defer and no subscription to trial."""
    access_start_floor()
    session = FakeSession(prior_payment_id=None)

    await StripeProvider.create_checkout(
        session, make_user(), make_plan(), gift_recipient_id=11, is_gift=True
    )

    payload = stripe_api.sessions[0]
    assert payload["mode"] == "payment"
    assert "subscription_data" not in payload
    assert "first_renewal_at" not in payload["metadata"]


@pytest.mark.asyncio
async def test_the_referral_discount_still_rides_on_the_prepaid_first_period(
    stripe_settings, stripe_api, access_start_floor
):
    """The discount is a `duration: once` coupon on the session, and with the
    floor on the first invoice is the prepaid line item — so the referred member
    still gets 20% off their first month and nothing off the renewals."""
    access_start_floor()
    session = FakeSession(prior_payment_id=None)

    await StripeProvider.create_checkout(session, make_user(referrer_id=99), make_plan())

    payload = stripe_api.sessions[0]
    assert payload["discounts"] == [{"coupon": "coupon_referral_20"}]
    assert payload["line_items"][0]["price_data"]["unit_amount"] == 1900, (
        "the line item stays at list price; the coupon is what discounts it, "
        "exactly as on the recurring path"
    )
    payment_row = next(o for o in session.added if isinstance(o, Payment))
    assert payment_row.amount == Decimal("15.20")


# ---------------------------------------------------------------------------
# GK-447: the coupon that could not be created
# ---------------------------------------------------------------------------
#
# `_ensure_referral_coupon` makes the referral coupon lazily, on the *first*
# referral checkout. Every test above monkeypatches it away, so the only thing
# that had ever run it was Stripe — and Stripe caps a coupon `name` at 40
# characters, while the shipped name was 56. The live account had never taken a
# referral checkout, so nothing had executed the line; the first referred member
# to press "buy" would have got a failed checkout instead of a discount.
#
# The fixture below is the missing half: a fake `Coupon` that enforces the
# provider's documented limits instead of accepting anything. Disable
# `_coupon_name` and these fail.


class _StripeCouponRejected(stripe_provider.stripe.error.InvalidRequestError):
    def __init__(self, message):
        super().__init__(message, param="name")
        self.http_status = 400


@pytest.fixture
def strict_coupon_api(monkeypatch):
    """A Stripe `Coupon` that refuses what the real one refuses."""
    created = []

    def fake_retrieve(coupon_id, **_kwargs):
        for c in created:
            if c["id"] == coupon_id:
                return SimpleNamespace(**c)
        missing = stripe_provider.stripe.error.InvalidRequestError(
            f"No such coupon: '{coupon_id}'", param="id"
        )
        missing.http_status = 404
        raise missing

    def fake_create(**kwargs):
        name = kwargs.get("name", "")
        if len(name) > stripe_provider.STRIPE_COUPON_NAME_MAX:
            raise _StripeCouponRejected(
                f"Invalid string: {name[:4]}...{name[-4:]}; must be at most "
                f"{stripe_provider.STRIPE_COUPON_NAME_MAX} characters"
            )
        created.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(stripe_provider.stripe.Coupon, "retrieve", fake_retrieve)
    monkeypatch.setattr(stripe_provider.stripe.Coupon, "create", fake_create)
    return created


def test_the_referral_coupon_can_actually_be_created(stripe_settings, strict_coupon_api):
    """The failure found by probing the live account before launch, 2026-08-15."""
    coupon_id = stripe_provider._ensure_referral_coupon()

    assert len(strict_coupon_api) == 1, "the coupon must be created, not 400'd"
    created = strict_coupon_api[0]
    assert created["id"] == coupon_id
    assert created["percent_off"] == 20.0
    assert created["duration"] == "once"
    assert len(created["name"]) <= stripe_provider.STRIPE_COUPON_NAME_MAX


def test_the_referral_coupon_is_made_once_and_then_reused(stripe_settings, strict_coupon_api):
    """Idempotent by lookup, so a second referral buyer does not re-create it."""
    first = stripe_provider._ensure_referral_coupon()
    second = stripe_provider._ensure_referral_coupon()

    assert first == second
    assert len(strict_coupon_api) == 1


def test_a_promo_code_long_enough_to_break_the_name_does_not_break_checkout(
    stripe_settings, strict_coupon_api
):
    """"membership_saas promo " is already 22 of the 40. An admin naming a promo
    `LAUNCH-SEPTEMBER-2026` is not making a mistake, and it must not cost a sale."""
    promo = SimpleNamespace(
        id=7,
        code="LAUNCH-SEPTEMBER-2026-EARLY-BIRD",
        discount_type="percent",
        percent_off=Decimal("15"),
        amount_off=None,
        amount_off_currency=None,
    )

    coupon_id = stripe_provider._ensure_promo_coupon(promo)

    assert len(strict_coupon_api) == 1
    created = strict_coupon_api[0]
    assert created["id"] == coupon_id
    assert len(created["name"]) <= stripe_provider.STRIPE_COUPON_NAME_MAX
    assert created["metadata"]["promo_code"] == promo.code, (
        "the full code stays in metadata — only the display label is clamped"
    )


@pytest.mark.parametrize(
    "name",
    [
        "short",
        "x" * stripe_provider.STRIPE_COUPON_NAME_MAX,
        "x" * (stripe_provider.STRIPE_COUPON_NAME_MAX + 1),
        "x" * 200,
    ],
)
def test_the_clamp_never_returns_something_stripe_would_reject(name):
    assert len(stripe_provider._coupon_name(name)) <= stripe_provider.STRIPE_COUPON_NAME_MAX
