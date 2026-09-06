from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db.models import Gift, Referral, ReferralCommission
from app.payments import fulfillment
from app.services.referral_ledger import record_referral_commission_intent


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, *scalar_values):
        self.scalar_values = list(scalar_values)

    async def execute(self, _query):
        # Trailing reads (e.g. the GK-402 consume lookup, whose "no active
        # reservation" answer is None) run after the queued values are spent.
        value = self.scalar_values.pop(0) if self.scalar_values else None
        return ScalarResult(value)


class FakeLedgerSession(FakeSession):
    def __init__(self, *scalar_values):
        super().__init__(*scalar_values)
        self.added = []
        self.flushed = False
        self._next_id = 100

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


def make_plan():
    return SimpleNamespace(id=2, code="monthly", duration_days=30)


def make_user(**overrides):
    data = {
        "id": 10,
        "tg_id": 10010,
        "referrer_id": 99,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_payment(**overrides):
    data = {
        "id": 1,
        "user_id": 10,
        "plan_id": 2,
        "provider": "stripe",
        "amount": 19,
        "currency": "USD",
        "status": "succeeded",
        "external_id": None,
        "is_gift": False,
        "gift_recipient_id": None,
        "created_at": datetime(2026, 5, 28, tzinfo=UTC),
        "approved_at": None,
        "provider_event_id": None,
        "stripe_invoice_id": None,
        "lava_invoice_id": None,
        "lava_subscription_id": None,
        "tx_hash": None,
        "tx_network": None,
        "is_renewal": False,
        "billing_period_start": None,
        "billing_period_end": None,
        "note": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_subscription(**overrides):
    data = {
        "id": 50,
        "expires_at": datetime(2026, 7, 1, tzinfo=UTC),
        "current_period_start": datetime(2026, 6, 1, tzinfo=UTC),
        "current_period_end": datetime(2026, 7, 1, tzinfo=UTC),
        "provider": "stripe",
        "provider_status": "active",
        "invite_link": "Community channel: invite-channel\nPractice chat: invite-chat",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture
def fulfillment_mocks(monkeypatch):
    calls = SimpleNamespace(
        invites=[],
        referrals=[],
        referral_coverages=[],
        subscriptions=[],
        dispatches=[],
        alerts=[],
    )

    async def fake_invite_links(_bot, name=None):
        calls.invites.append(name)
        return SimpleNamespace(
            storage_text="Community channel: invite-channel\nPractice chat: invite-chat",
            all_success=True,
            error_summary=None,
        )

    async def fake_referral(session, recipient, payment, **coverage):
        calls.referrals.append((session, recipient, payment))
        calls.referral_coverages.append(coverage)

    async def fake_create_subscription(session, **kwargs):
        calls.subscriptions.append((session, kwargs))
        return make_subscription(
            provider=kwargs.get("provider"),
            provider_status=kwargs.get("provider_status"),
            invite_link=kwargs.get("invite_link"),
        )

    async def fake_dispatch(session, event, payload):
        calls.dispatches.append((session, event, payload))

    async def fake_alert(text, **kwargs):
        calls.alerts.append((text, kwargs))
        return True

    monkeypatch.setattr(fulfillment, "create_invite_links", fake_invite_links)
    monkeypatch.setattr(fulfillment, "record_referral_commission_intent", fake_referral)
    monkeypatch.setattr(fulfillment, "create_or_extend_subscription", fake_create_subscription)
    monkeypatch.setattr(fulfillment, "dispatch", fake_dispatch)
    monkeypatch.setattr(fulfillment, "send_ops_alert", fake_alert)
    return calls


@pytest.mark.asyncio
async def test_first_payment_grants_both_resources_and_records_referral_intent(fulfillment_mocks):
    payment = make_payment(external_id="sub_123")
    recipient = make_user()
    session = FakeSession(make_plan(), recipient)

    sub = await fulfillment.fulfill_payment(session, object(), payment)

    assert sub.id == 50
    assert payment.approved_at is not None
    assert fulfillment_mocks.invites == ["membership_saas pay#1"]
    assert fulfillment_mocks.referrals == [(session, recipient, payment)]
    _, sub_kwargs = fulfillment_mocks.subscriptions[0]
    assert sub_kwargs["user"] is recipient
    assert sub_kwargs["source"] == "stripe"
    assert sub_kwargs["provider"] == "stripe"
    assert sub_kwargs["provider_subscription_id"] == "sub_123"
    assert sub_kwargs["invite_link"] == "Community channel: invite-channel\nPractice chat: invite-chat"
    assert [event for _, event, _ in fulfillment_mocks.dispatches] == [
        "payment.succeeded",
        "subscription.activated",
    ]


@pytest.mark.asyncio
async def test_partial_telegram_grant_is_recorded_without_rolling_back(monkeypatch, fulfillment_mocks):
    payment = make_payment(external_id="sub_123")
    recipient = make_user(referrer_id=None)
    session = FakeSession(make_plan(), recipient)

    async def partial_invite_links(_bot, name=None):
        fulfillment_mocks.invites.append(name)
        return SimpleNamespace(
            storage_text="Community channel: invite-channel",
            all_success=False,
            error_summary="practice_chat: bot is not admin",
        )

    monkeypatch.setattr(fulfillment, "create_invite_links", partial_invite_links)

    sub = await fulfillment.fulfill_payment(session, object(), payment)

    assert sub.id == 50
    assert payment.approved_at is not None
    assert "practice_chat: bot is not admin" in payment.note
    _, sub_kwargs = fulfillment_mocks.subscriptions[0]
    assert sub_kwargs["invite_link"] == "Community channel: invite-channel"
    assert len(fulfillment_mocks.alerts) == 1
    alert_text, alert_kwargs = fulfillment_mocks.alerts[0]
    assert "Доступ в Telegram выдан не полностью" in alert_text
    assert "practice_chat: bot is not admin" in alert_text
    assert alert_kwargs == {
        "key": "payment_invite_partial:1",
        "rate_limit_seconds": 3600,
        "severity": "warn",
    }


@pytest.mark.asyncio
async def test_total_telegram_grant_failure_pages_ops_and_fulfills(
    monkeypatch, fulfillment_mocks
):
    payment = make_payment(external_id="sub_123")
    recipient = make_user(referrer_id=None)
    session = FakeSession(make_plan(), recipient)

    async def failed_invite_links(_bot, name=None):
        fulfillment_mocks.invites.append(name)
        return SimpleNamespace(
            storage_text=None,
            all_success=False,
            error_summary="community_channel: bot is not admin & chat unavailable",
        )

    monkeypatch.setattr(fulfillment, "create_invite_links", failed_invite_links)

    sub = await fulfillment.fulfill_payment(session, object(), payment)

    assert sub.id == 50
    assert payment.approved_at is not None
    assert "community_channel: bot is not admin" in payment.note
    _, sub_kwargs = fulfillment_mocks.subscriptions[0]
    assert sub_kwargs["invite_link"] is None
    assert len(fulfillment_mocks.alerts) == 1
    alert_text, alert_kwargs = fulfillment_mocks.alerts[0]
    assert "Не удалось создать ни одной ссылки Telegram" in alert_text
    assert "payment_id=1" in alert_text
    # A plain `&`, not `&amp;`. The error summary is Telegram's own text and
    # this one carries an ampersand on purpose: the call site no longer escapes,
    # `send_ops_alert` escapes the body once on the way out, and the member of
    # ops reading the alert sees the sentence Telegram actually returned. The
    # previous expectation here was the double-escape written down as a test.
    assert "bot is not admin & chat unavailable" in alert_text
    assert alert_kwargs == {
        "key": "payment_invite_total:1",
        "rate_limit_seconds": 3600,
        "severity": "error",
    }


@pytest.mark.asyncio
async def test_invite_failure_alert_error_does_not_break_fulfillment(
    monkeypatch, fulfillment_mocks
):
    payment = make_payment()
    recipient = make_user(referrer_id=None)
    session = FakeSession(make_plan(), recipient)

    async def failed_invite_links(_bot, name=None):
        return SimpleNamespace(
            storage_text=None,
            all_success=False,
            error_summary="Telegram unavailable",
        )

    async def failed_alert(*_args, **_kwargs):
        raise RuntimeError("alert transport unavailable")

    monkeypatch.setattr(fulfillment, "create_invite_links", failed_invite_links)
    monkeypatch.setattr(fulfillment, "send_ops_alert", failed_alert)

    sub = await fulfillment.fulfill_payment(session, object(), payment)

    assert sub.id == 50
    assert payment.approved_at is not None
    _, sub_kwargs = fulfillment_mocks.subscriptions[0]
    assert sub_kwargs["invite_link"] is None


@pytest.mark.asyncio
async def test_local_duplicate_payment_skips_side_effects(fulfillment_mocks):
    payment = make_payment(approved_at=datetime(2026, 5, 28, tzinfo=UTC))

    sub = await fulfillment.fulfill_payment(FakeSession(), object(), payment)

    assert sub is None
    assert fulfillment_mocks.invites == []
    assert fulfillment_mocks.referrals == []
    assert fulfillment_mocks.subscriptions == []
    assert fulfillment_mocks.dispatches == []


@pytest.mark.asyncio
async def test_provider_duplicate_invoice_skips_side_effects(monkeypatch, fulfillment_mocks):
    fulfilled_at = datetime(2026, 5, 28, tzinfo=UTC)
    duplicate = make_payment(id=99, approved_at=fulfilled_at)
    payment = make_payment(id=100, stripe_invoice_id="in_123")

    async def fake_duplicate(_session, candidate):
        assert candidate is payment
        return duplicate

    monkeypatch.setattr(fulfillment, "_find_fulfilled_provider_duplicate", fake_duplicate)

    sub = await fulfillment.fulfill_payment(FakeSession(), object(), payment)

    assert sub is None
    assert payment.approved_at == fulfilled_at
    assert fulfillment_mocks.invites == []
    assert fulfillment_mocks.referrals == []
    assert fulfillment_mocks.subscriptions == []
    assert fulfillment_mocks.dispatches == []


@pytest.mark.asyncio
async def test_renewal_extends_recipient_without_invite_and_records_commission(fulfillment_mocks):
    period_start = datetime(2026, 6, 1, tzinfo=UTC)
    period_end = period_start + timedelta(days=30)
    payment = make_payment(
        is_renewal=True,
        billing_period_start=period_start,
        billing_period_end=period_end,
    )
    recipient = make_user()
    session = FakeSession(make_plan(), recipient)

    await fulfillment.fulfill_payment(session, object(), payment)

    assert fulfillment_mocks.invites == []
    assert fulfillment_mocks.referrals == [(session, recipient, payment)]
    assert fulfillment_mocks.referral_coverages == [
        {"coverage_start": period_start, "coverage_end": period_end}
    ]
    _, sub_kwargs = fulfillment_mocks.subscriptions[0]
    assert sub_kwargs["user"] is recipient
    assert sub_kwargs["invite_link"] is None
    assert sub_kwargs["current_period_start"] == period_start
    assert sub_kwargs["current_period_end"] == period_end
    assert [event for _, event, _ in fulfillment_mocks.dispatches] == [
        "payment.succeeded",
        "subscription.renewed",
    ]


@pytest.mark.asyncio
async def test_gift_fulfills_recipient_without_provider_subscription(fulfillment_mocks):
    gift_recipient = make_user(id=30, tg_id=30030, referrer_id=99)
    payment = make_payment(
        is_gift=True,
        gift_recipient_id=gift_recipient.id,
        provider="lava",
        lava_subscription_id="lava_sub_should_not_attach",
        billing_period_start=datetime(2026, 6, 1, tzinfo=UTC),
        billing_period_end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session = FakeSession(make_plan(), gift_recipient)

    await fulfillment.fulfill_payment(session, object(), payment)

    assert fulfillment_mocks.referrals == []
    _, sub_kwargs = fulfillment_mocks.subscriptions[0]
    assert sub_kwargs["user"] is gift_recipient
    assert sub_kwargs["source"] == "gift"
    assert sub_kwargs["provider"] is None
    assert sub_kwargs["provider_subscription_id"] is None
    assert sub_kwargs["invite_link"] == "Community channel: invite-channel\nPractice chat: invite-chat"
    assert sub_kwargs["current_period_start"] is None
    assert sub_kwargs["current_period_end"] is None


@pytest.mark.asyncio
async def test_unclaimed_gift_payment_creates_activation_record_without_subscription(fulfillment_mocks):
    buyer = make_user(id=10, tg_id=10010, referrer_id=None)
    payment = make_payment(is_gift=True, gift_recipient_id=None)
    session = FakeLedgerSession(make_plan(), buyer, None)

    sub = await fulfillment.fulfill_payment(session, object(), payment)

    assert sub is None
    assert payment.approved_at is not None
    assert fulfillment_mocks.invites == []
    assert fulfillment_mocks.referrals == []
    assert fulfillment_mocks.subscriptions == []
    gift = next(obj for obj in session.added if isinstance(obj, Gift))
    assert gift.sender_id == buyer.id
    assert gift.receiver_id is None
    assert gift.plan_id == payment.plan_id
    assert gift.payment_id == payment.id
    assert [event for _, event, _ in fulfillment_mocks.dispatches] == ["payment.succeeded"]
    assert fulfillment_mocks.dispatches[0][2]["gift_id"] == gift.id


@pytest.mark.asyncio
async def test_manual_payment_fulfills_even_without_bot_invite(fulfillment_mocks):
    payment = make_payment(provider="usdt")
    recipient = make_user(referrer_id=None)
    session = FakeSession(make_plan(), recipient)

    await fulfillment.fulfill_payment(session, None, payment)

    assert payment.approved_at is not None
    assert fulfillment_mocks.invites == []
    _, sub_kwargs = fulfillment_mocks.subscriptions[0]
    assert sub_kwargs["user"] is recipient
    assert sub_kwargs["source"] == "usdt"
    assert sub_kwargs["provider"] == "usdt"
    assert sub_kwargs["invite_link"] is None


@pytest.mark.asyncio
async def test_referral_ledger_intent_does_not_mutate_legacy_bonus_days():
    referrer = make_user(id=99)
    referrer.bonus_days = 7
    referee = make_user(id=10, referrer_id=referrer.id)
    payment = make_payment(amount=19, stripe_invoice_id="in_first")
    session = FakeLedgerSession(None, referrer, None, None)

    commission = await record_referral_commission_intent(session, referee, payment)

    ref = next(obj for obj in session.added if isinstance(obj, Referral))
    assert commission is next(obj for obj in session.added if isinstance(obj, ReferralCommission))
    assert ref.referrer_id == referrer.id
    assert ref.referee_id == referee.id
    assert ref.first_payment_id == payment.id
    assert ref.bonus_days_granted == 0
    assert commission.referral_id == ref.id
    assert commission.source_payment_id == payment.id
    assert commission.source_invoice_id == "in_first"
    assert float(commission.amount_usd) == 3.8
    assert commission.status == "pending"
    assert referrer.bonus_days == 7
    assert session.flushed is True


@pytest.mark.asyncio
async def test_gift_payment_never_enters_recurring_partner_ledger():
    referee = make_user(id=30, referrer_id=99)
    payment = make_payment(
        id=90,
        user_id=10,
        gift_recipient_id=referee.id,
        is_gift=True,
        is_renewal=True,
        amount=149,
    )
    session = FakeLedgerSession()

    commission = await record_referral_commission_intent(session, referee, payment)

    assert commission is None
    assert session.added == []
    assert session.flushed is False
