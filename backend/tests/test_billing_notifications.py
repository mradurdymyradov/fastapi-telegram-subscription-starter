import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import billing_notifications


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _query):
        if not self.values:
            raise AssertionError("FakeSession.execute called without queued result")
        return ScalarResult(self.values.pop(0))


def make_user(**overrides):
    data = {"id": 10, "tg_id": 10010}
    data.update(overrides)
    return SimpleNamespace(**data)


def make_plan(**overrides):
    data = {"id": 20, "name": "Monthly Access", "code": "1m"}
    data.update(overrides)
    return SimpleNamespace(**data)


def make_payment(**overrides):
    data = {
        "id": 123,
        "user_id": 10,
        "gift_recipient_id": None,
        "plan_id": 20,
        "provider": "stripe",
        "amount": Decimal("19.00"),
        "currency": "USD",
        "is_gift": False,
        "is_renewal": False,
        "billing_period_end": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_subscription(**overrides):
    data = {
        "id": 50,
        "user_id": 10,
        "plan_id": 20,
        "provider": "stripe",
        "current_period_end": datetime(2026, 7, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 7, 1, tzinfo=UTC),
        "grace_ends_at": None,
        "invite_link": "https://t.me/+invite",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture
def sent_messages(monkeypatch):
    sent = []

    async def fake_send_message(tg_id, text, reply_markup=None):
        sent.append((tg_id, text, reply_markup))
        return True

    monkeypatch.setattr(billing_notifications, "send_message", fake_send_message)
    return sent


@pytest.mark.asyncio
async def test_initial_payment_notification_shows_plan_amount_next_date_and_invite(sent_messages):
    payment = make_payment()
    subscription = make_subscription()

    ok = await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()),
        payment,
        subscription,
    )

    assert ok is True
    assert len(sent_messages) == 1
    tg_id, text, _markup = sent_messages[0]
    assert tg_id == 10010
    assert "Оплата получена" in text
    assert "1 месяц" in text
    assert "Monthly Access" not in text
    assert "$19.00" in text
    assert "2026-07-01" in text
    assert "https://t.me/+invite" in text


@pytest.mark.asyncio
async def test_renewal_notification_shows_plan_amount_and_next_date(sent_messages):
    payment = make_payment(
        is_renewal=True,
        billing_period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )

    ok = await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()),
        payment,
        make_subscription(invite_link=None),
    )

    assert ok is True
    text = sent_messages[0][1]
    assert "Подписка продлена" in text
    assert "1 месяц" in text
    assert "Monthly Access" not in text
    assert "$19.00" in text
    assert "2026-08-01" in text
    assert "Ссылки для входа" not in text


@pytest.mark.asyncio
async def test_gift_notification_gives_recipient_clear_access_message(sent_messages):
    payment = make_payment(
        is_gift=True,
        user_id=11,
        gift_recipient_id=30,
        amount=Decimal("89.00"),
    )

    ok = await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(id=30, tg_id=30030), make_plan(name="Six Month Access")),
        payment,
        make_subscription(user_id=30, invite_link="gift-invite"),
    )

    assert ok is True
    tg_id, text, _markup = sent_messages[0]
    assert tg_id == 30030
    assert "Вам подарили подписку membership_saas" in text
    assert "6 месяцев" in text
    assert "Six Month Access" not in text
    assert "$89.00" in text
    assert "gift-invite" in text


@pytest.mark.asyncio
async def test_unclaimed_gift_notification_sends_activation_link_to_buyer(monkeypatch, sent_messages):
    payment = make_payment(is_gift=True, gift_recipient_id=None)
    gift = SimpleNamespace(id=77, created_at=datetime(2026, 6, 19, tzinfo=UTC))

    async def fake_gift_activation(_session, paid):
        assert paid is payment
        return gift

    monkeypatch.setattr(
        billing_notifications,
        "ensure_paid_gift_activation",
        fake_gift_activation,
    )

    ok = await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()),
        payment,
        None,
    )

    assert ok is True
    tg_id, text, _markup = sent_messages[0]
    assert tg_id == 10010
    assert "start=gift_77_" in text
    assert "2026-07-19" in text
    assert "Подарочная подписка оплачена" in text


@pytest.mark.asyncio
async def test_failed_renewal_notification_mentions_grace_without_double_pay_instruction(sent_messages):
    payment = make_payment(is_renewal=True)
    subscription = make_subscription(
        grace_ends_at=datetime(2026, 7, 4, tzinfo=UTC),
        invite_link=None,
    )

    ok = await billing_notifications.notify_payment_failed(
        FakeSession(make_user(), make_plan()),
        payment=payment,
        subscription=subscription,
        provider="stripe",
    )

    assert ok is True
    text = sent_messages[0][1]
    assert "Нужно внимание к продлению подписки" in text
    assert "Stripe" in text
    assert "1 месяц" in text
    assert "Monthly Access" not in text
    assert "$19.00" in text
    assert "2026-07-04" in text
    assert "не оплачивайте дважды" in text


@pytest.mark.asyncio
async def test_cancellation_notification_keeps_paid_period_language(sent_messages):
    subscription = make_subscription(current_period_end=datetime(2026, 7, 1, tzinfo=UTC))

    ok = await billing_notifications.notify_subscription_cancelled(
        FakeSession(make_user(), make_plan()),
        subscription,
        provider="lava",
    )

    assert ok is True
    text = sent_messages[0][1]
    assert "Получена отмена подписки" in text
    assert "Lava" in text
    assert "1 месяц" in text
    assert "Monthly Access" not in text
    assert "Доступ сохраняется до" in text
    assert "2026-07-01" in text


@pytest.mark.asyncio
async def test_archive_password_update_notification_uses_portal_language(sent_messages):
    ok = await billing_notifications.notify_archive_password_updated(
        10010,
        portal_url="https://community.example.test/portal",
    )

    assert ok is True
    tg_id, text, _markup = sent_messages[0]
    assert tg_id == 10010
    assert "Данные доступа к архиву обновлены" in text
    assert "Старые пароли или ссылки могут перестать работать" in text
    assert "https://community.example.test/portal" in text


@pytest.mark.asyncio
async def test_usdt_expiry_reminder_states_paid_through_date_and_manual_renewal(sent_messages):
    markup = object()

    ok = await billing_notifications.notify_usdt_expiring(
        10010,
        datetime(2026, 6, 23, tzinfo=UTC),
        reply_markup=markup,
    )

    assert ok is True
    tg_id, text, sent_markup = sent_messages[0]
    assert tg_id == 10010
    assert "USDT" in text
    assert "2026-06-23" in text
    assert "Автоматического списания не будет" in text
    assert "вручную" in text
    assert sent_markup is markup


@pytest.mark.asyncio
async def test_send_failure_is_logged_and_swallowed(monkeypatch, caplog):
    async def fail_send_message(*_args, **_kwargs):
        raise RuntimeError("telegram unavailable")

    monkeypatch.setattr(billing_notifications, "send_message", fail_send_message)
    caplog.set_level(logging.ERROR)

    ok = await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()),
        make_payment(),
        make_subscription(),
    )

    assert ok is False
    assert "continuing billing flow" in caplog.text
