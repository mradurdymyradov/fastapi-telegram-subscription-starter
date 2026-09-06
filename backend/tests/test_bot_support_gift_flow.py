import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import _include_member_routers
from app.bot.handlers import gift as gift_handlers
from app.bot.handlers import subscription as subscription_handlers
from app.bot.handlers import support as support_handlers
from app.db.models import Gift, Subscription
from app.services import gifts
from app.services import subscription as subscription_service

NOW = datetime(2026, 6, 19, tzinfo=UTC)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class FakeSession:
    def __init__(self, *values):
        self.values = list(values)
        self.added = []
        self.flushes = 0

    async def execute(self, _query):
        if not self.values:
            raise AssertionError("FakeSession.execute called without queued result")
        return Result(self.values.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 700 + self.flushes
            if getattr(obj, "created_at", None) is None:
                obj.created_at = NOW


def plan(**overrides):
    data = {
        "id": 20,
        "code": "1m",
        "name": "1 месяц",
        "price_rub": Decimal("0"),
        "price_usd": Decimal("19.00"),
        "duration_days": 30,
        "is_active": True,
        "sort_order": 1,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def user(**overrides):
    data = {
        "id": 44,
        "tg_id": 44044,
        "username": "recipient",
        "referrer_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def payment(**overrides):
    data = {
        "id": 90,
        "user_id": 10,
        "plan_id": 20,
        "provider": "stripe",
        "amount": Decimal("19.00"),
        "currency": "USD",
        "status": "succeeded",
        "is_gift": True,
        "gift_recipient_id": None,
        "approved_at": NOW,
        "billing_period_start": None,
        "billing_period_end": None,
        "note": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def active_subscription(**overrides):
    data = {
        "id": 55,
        "user_id": 44,
        "plan_id": 20,
        "status": "active",
        "source": "stripe",
        "started_at": NOW - timedelta(days=20),
        "expires_at": NOW + timedelta(days=10),
        "invite_link": "existing-invite",
        "notified_expiring": False,
        "provider": "stripe",
        "provider_subscription_id": "sub_existing",
        "provider_status": "active",
        "current_period_start": NOW - timedelta(days=20),
        "current_period_end": NOW + timedelta(days=10),
        "cancel_at_period_end": False,
        "grace_started_at": None,
        "grace_ends_at": None,
        "access_revoked_at": None,
        "access_revoke_retry_after_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def callback(data: str):
    return SimpleNamespace(data=data, message=AsyncMock(), answer=AsyncMock())


def button_callback_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_support_entry_clears_gift_state_and_uses_configured_contact(monkeypatch):
    message = AsyncMock()
    state = AsyncMock()
    monkeypatch.setattr(support_handlers, "settings", SimpleNamespace(support_contact="@GKcurators"))

    await support_handlers.support_entry(message, state)

    state.clear.assert_awaited_once()
    state.set_state.assert_awaited_once_with(support_handlers.SupportFlow.chatting)
    text = message.answer.await_args.args[0]
    assert "@GKcurators" in text
    assert "/cancel" in text
    assert message.answer.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_support_cancel_exits_dialog_with_menu():
    message = AsyncMock()
    state = AsyncMock()

    await support_handlers.cancel(message, state)

    state.clear.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "закрыт" in text
    assert message.answer.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_gift_entry_selects_plan_without_recipient_username_requirement():
    message = AsyncMock()
    state = AsyncMock()

    await gift_handlers.gift_entry(message, FakeSession([plan()]), state)

    state.update_data.assert_awaited_with(
        plan_id=None,
        gift_purchase=True,
        gift_recipient_id=None,
        promo_code=None,
    )
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.choosing_plan)
    text = message.answer.await_args.args[0]
    assert "@username" not in text
    assert "заранее указывать не нужно" in text


@pytest.mark.asyncio
async def test_gift_plan_list_is_limited_to_active_launch_terms():
    plans = [
        plan(id=1, code="1m", duration_days=30),
        plan(id=2, code="6m", duration_days=180),
        plan(id=3, code="12m", duration_days=365),
        plan(id=4, code="3m", duration_days=90),
        plan(id=5, code="1m", duration_days=30, is_active=False),
        plan(id=6, code="12m", duration_days=30),
    ]

    result = await gift_handlers._active_plans(FakeSession(plans))

    assert [item.code for item in result] == ["1m", "6m", "12m"]


@pytest.mark.asyncio
async def test_gift_plan_callback_rejects_non_launch_term():
    cb = callback("gift_plan:20")
    state = AsyncMock()

    await gift_handlers.gift_plan_chosen(
        cb,
        FakeSession(plan(code="3m", duration_days=90)),
        state,
    )

    cb.answer.assert_awaited_once_with("Этот подарочный тариф недоступен", show_alert=True)
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_gift_plan_payment_methods_hide_promocode_and_keep_gift_state():
    cb = callback("gift_plan:20")
    state = AsyncMock()

    await gift_handlers.gift_plan_chosen(cb, FakeSession(plan()), state)

    state.update_data.assert_awaited_with(
        plan_id=20,
        gift_purchase=True,
        gift_recipient_id=None,
        promo_code=None,
    )
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.choosing_method)
    text = cb.message.edit_text.await_args.args[0]
    assert "одноразовую ссылку активации" in text
    callbacks = button_callback_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert "promo:20" not in callbacks
    assert "gift_back_to_plans" in callbacks


@pytest.mark.asyncio
async def test_payment_callback_passes_unclaimed_gift_flag_to_provider(monkeypatch):
    cb = callback("pm:stripe:20")
    state = AsyncMock()
    state.get_data.return_value = {
        "plan_id": 20,
        "gift_purchase": True,
        "gift_recipient_id": None,
        "promo_code": "SHOULD_NOT_APPLY",
    }
    captured = []

    async def fake_checkout(_session, _user, chosen_plan, gift_recipient_id, *, is_gift=False, promo_code=None):
        captured.append((chosen_plan, gift_recipient_id, is_gift, promo_code))
        return SimpleNamespace(url="https://checkout.example.test/gift")

    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", fake_checkout)
    monkeypatch.setattr(subscription_handlers, "settings", SimpleNamespace(enable_zelle=False))

    await subscription_handlers.payment_method_chosen(
        cb,
        FakeSession(plan()),
        state,
        user(id=10),
    )

    assert captured == [(plan(), None, True, None)]
    state.update_data.assert_awaited_with(
        plan_id=20,
        gift_purchase=True,
        gift_recipient_id=None,
        promo_code=None,
    )


@pytest.mark.asyncio
async def test_payment_callback_rejects_tampered_non_launch_gift_plan(monkeypatch):
    cb = callback("pm:stripe:20")
    state = AsyncMock()
    state.get_data.return_value = {
        "plan_id": 20,
        "gift_purchase": True,
        "gift_recipient_id": None,
        "promo_code": None,
    }
    checkout = AsyncMock(side_effect=AssertionError("unsupported gift must not reach Stripe"))
    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", checkout)

    await subscription_handlers.payment_method_chosen(
        cb,
        FakeSession(plan(code="3m", duration_days=90)),
        state,
        user(id=10),
    )

    checkout.assert_not_awaited()
    state.clear.assert_awaited_once()
    cb.answer.assert_awaited_once_with("Этот подарочный тариф недоступен", show_alert=True)


@pytest.mark.asyncio
async def test_ensure_paid_gift_activation_creates_one_gift_record():
    paid = payment()
    session = FakeSession(None)

    gift = await gifts.ensure_paid_gift_activation(session, paid)

    assert isinstance(gift, Gift)
    assert gift.sender_id == paid.user_id
    assert gift.receiver_id is None
    assert gift.plan_id == paid.plan_id
    assert gift.payment_id == paid.id
    assert gift.id == 701


@pytest.mark.asyncio
async def test_ensure_paid_gift_activation_reuses_payment_record():
    existing = Gift(
        id=7,
        sender_id=10,
        receiver_id=None,
        plan_id=20,
        payment_id=90,
        created_at=NOW,
    )
    session = FakeSession(existing)

    gift = await gifts.ensure_paid_gift_activation(session, payment())

    assert gift is existing
    assert session.added == []


@pytest.mark.asyncio
async def test_redeem_gift_token_assigns_receiver_and_grants_subscription(monkeypatch):
    gift = Gift(
        id=7,
        sender_id=10,
        receiver_id=None,
        plan_id=20,
        payment_id=90,
        created_at=NOW,
    )
    paid = payment()
    granted = []
    dispatched = []

    async def fake_invites(_bot, name=None):
        granted.append(name)
        return SimpleNamespace(
            storage_text="Community channel: invite-channel\nPractice chat: invite-chat",
            all_success=True,
            error_summary=None,
        )

    async def fake_subscription(_session, **kwargs):
        return SimpleNamespace(id=55, expires_at=NOW + timedelta(days=30), **kwargs)

    async def fake_dispatch(_session, event, payload):
        dispatched.append((event, payload))

    monkeypatch.setattr(gifts, "create_invite_links", fake_invites)
    monkeypatch.setattr(gifts, "create_or_extend_subscription", fake_subscription)
    monkeypatch.setattr(gifts, "dispatch", fake_dispatch)

    token = gifts.gift_activation_token(gift)
    result = await gifts.redeem_gift_token(
        FakeSession(gift, paid, plan()),
        token,
        user(),
        bot=object(),
        now=NOW,
    )

    assert result.ok is True
    assert gift.receiver_id == 44
    assert gift.redeemed_at == NOW
    assert paid.gift_recipient_id == 44
    assert result.invite_link == "Community channel: invite-channel\nPractice chat: invite-chat"
    assert granted == ["membership_saas gift#7"]
    assert dispatched[0][0] == "subscription.activated"
    assert result.subscription.current_period_start is None
    assert result.subscription.current_period_end is None


@pytest.mark.asyncio
async def test_redeem_gift_stacks_full_duration_after_active_paid_through(monkeypatch):
    gift = Gift(
        id=7,
        sender_id=10,
        receiver_id=None,
        plan_id=20,
        payment_id=90,
        created_at=NOW - timedelta(days=20),
    )
    paid = payment(
        billing_period_start=NOW - timedelta(days=20),
        billing_period_end=NOW + timedelta(days=10),
    )
    current = active_subscription()
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)
    monkeypatch.setattr(gifts, "dispatch", AsyncMock())

    result = await gifts.redeem_gift_token(
        FakeSession(gift, paid, plan(), [current]),
        gifts.gift_activation_token(gift),
        user(),
        now=NOW,
    )

    assert result.ok is True
    assert result.subscription is current
    assert current.expires_at == NOW + timedelta(days=40)
    assert current.current_period_end == NOW + timedelta(days=40)


@pytest.mark.asyncio
async def test_redeem_gift_activates_full_duration_for_inactive_recipient(monkeypatch):
    gift = Gift(
        id=7,
        sender_id=10,
        receiver_id=None,
        plan_id=20,
        payment_id=90,
        created_at=NOW - timedelta(days=20),
    )
    paid = payment(
        billing_period_start=NOW - timedelta(days=20),
        billing_period_end=NOW + timedelta(days=10),
    )
    session = FakeSession(gift, paid, plan(), [])
    monkeypatch.setattr(subscription_service, "utcnow", lambda: NOW)
    monkeypatch.setattr(gifts, "dispatch", AsyncMock())

    result = await gifts.redeem_gift_token(
        session,
        gifts.gift_activation_token(gift),
        user(),
        now=NOW,
    )

    assert result.ok is True
    assert isinstance(result.subscription, Subscription)
    assert result.subscription.started_at == NOW
    assert result.subscription.expires_at == NOW + timedelta(days=30)


@pytest.mark.asyncio
async def test_redeem_gift_rejects_second_use():
    gift = Gift(
        id=7,
        sender_id=10,
        receiver_id=44,
        plan_id=20,
        payment_id=90,
        redeemed_at=NOW,
        created_at=NOW - timedelta(days=1),
    )

    result = await gifts.redeem_gift_token(
        FakeSession(gift),
        gifts.gift_activation_token(gift),
        user(),
        now=NOW,
    )

    assert result.status == "already_redeemed"


@pytest.mark.asyncio
async def test_redeem_gift_expires_at_30_day_boundary():
    gift = Gift(
        id=7,
        sender_id=10,
        receiver_id=None,
        plan_id=20,
        payment_id=90,
        created_at=NOW - timedelta(days=30),
    )

    result = await gifts.redeem_gift_token(
        FakeSession(gift),
        gifts.gift_activation_token(gift),
        user(),
        now=NOW,
    )

    assert result.status == "expired"


def test_gift_link_expires_in_configured_30_day_window():
    gift = Gift(id=7, sender_id=10, receiver_id=None, plan_id=20, payment_id=90, created_at=NOW)

    assert gifts.gift_expires_at(gift) == NOW + timedelta(days=30)


def test_gift_router_precedes_support_router_so_the_gift_button_is_reachable():
    # GK-375 put support first because gift.py then owned a catch-all state
    # (`GiftFlow.waiting_recipient`) that ate `/support`. That same commit
    # deleted the state, and the ordering it justified went on to eat
    # «🎁 Подарить» instead — Grant's 19.08 report. GK-453 puts it back:
    # support's two catch-alls claim all free text, so nothing that answers
    # plain text may be registered behind them.
    source = inspect.getsource(_include_member_routers)

    assert source.index("include_router(gift.router)") < source.index("include_router(support.router)")
    assert source.index("include_router(fallback)") > source.index("include_router(support.router)")
