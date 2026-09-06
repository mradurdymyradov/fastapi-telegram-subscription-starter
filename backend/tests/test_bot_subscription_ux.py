from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import keyboards
from app.bot.handlers import subscription as subscription_handlers
from app.db.models import SupportMessage
from app.payments import lava_provider
from app.payments.lava_provider import LavaProvider
from app.payments.stripe_provider import StripeCancellationError, StripeProvider


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
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        if not self.values:
            raise AssertionError("FakeSession.execute called without queued result")
        return Result(self.values.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 900 + self.flushes


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
        "id": 10,
        "tg_id": 10010,
        "username": "member",
        "referrer_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def callback(data: str):
    return SimpleNamespace(data=data, message=AsyncMock(), answer=AsyncMock())


def active_subscription(**overrides):
    data = {
        "id": 50,
        "user_id": 10,
        "plan_id": 20,
        "status": "active",
        "source": "stripe",
        "provider": "stripe",
        "provider_subscription_id": "sub_123",
        "provider_status": "active",
        "current_period_end": datetime(2026, 7, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 7, 1, tzinfo=UTC),
        "grace_ends_at": None,
        "cancel_at_period_end": False,
        "access_revoked_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def test_plan_labels_are_dollar_first_and_hide_zero_rub():
    zero_rub = plan()
    fixed_rub = plan(id=21, name="6 месяцев", price_rub=Decimal("7000"), price_usd=Decimal("79"))

    markup = keyboards.plans_keyboard([zero_rub, fixed_rub])
    labels = button_texts(markup)

    assert labels[0] == "1 месяц - $19"
    assert "0 ₽" not in labels[0]
    assert labels[1] == "6 месяцев - $79 / 7000 ₽"
    assert labels[1].index("$79") < labels[1].index("7000 ₽")
    assert labels[-1] == "🔙 Назад"


def test_payment_keyboard_uses_lava_ru_methods_label_and_back_navigation():
    markup = keyboards.payment_method_keyboard("20", applied_promo="WELCOME20")
    labels = button_texts(markup)

    assert "🇷🇺 Lava Top (карта РФ / СБП, ₽)" in labels
    assert all("по курсу" not in text for text in labels)
    assert "🎟 Промокод: WELCOME20" in labels
    assert "🔙 Назад" in labels
    assert all("Отмена" not in text for text in labels)


def test_payment_keyboard_offers_offer_agreement_for_every_method():
    # The "📄 Договор оферты" button is method-agnostic: it lives on the shared
    # payment-method keyboard, so it shows for Stripe / Lava / USDT alike, with
    # and without an applied promo or Zelle enabled.
    for kwargs in (
        {},
        {"applied_promo": "WELCOME20"},
        {"enable_zelle": True},
        {"allow_promo": False},
    ):
        markup = keyboards.payment_method_keyboard("20", **kwargs)
        pairs = [
            (button.text, button.callback_data)
            for row in markup.inline_keyboard
            for button in row
        ]
        assert ("📄 Договор оферты", "offer_doc") in pairs


@pytest.mark.asyncio
async def test_offer_document_button_sends_pdf_without_disturbing_screen():
    cb = callback("offer_doc")
    cb.message.answer_document = AsyncMock()

    await subscription_handlers.offer_document_cb(cb)

    cb.message.answer_document.assert_awaited_once()
    sent = cb.message.answer_document.await_args
    document = sent.args[0]
    assert document.filename == subscription_handlers.OFFER_DOCUMENT_FILENAME
    assert "оферты" in sent.kwargs["caption"]
    # The payment screen itself is untouched — the PDF is a fresh message.
    cb.message.edit_text.assert_not_awaited()
    cb.answer.assert_awaited_once_with()


def test_offer_document_asset_is_shipped_in_repo():
    # The Dockerfile copies app/ into the image; the static asset must exist so
    # the runtime button never 404s.
    assert subscription_handlers.OFFER_DOCUMENT_PATH.exists()
    assert subscription_handlers.OFFER_DOCUMENT_PATH.suffix == ".pdf"


@pytest.mark.asyncio
async def test_offer_document_button_warns_when_asset_missing(monkeypatch, tmp_path):
    cb = callback("offer_doc")
    cb.message.answer_document = AsyncMock()
    monkeypatch.setattr(
        subscription_handlers,
        "OFFER_DOCUMENT_PATH",
        tmp_path / "missing.pdf",
    )

    await subscription_handlers.offer_document_cb(cb)

    cb.message.answer_document.assert_not_awaited()
    assert cb.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_buy_plan_screen_previews_referral_discount_and_rate_based_lava(monkeypatch):
    cb = callback("buy_plan:20")
    state = AsyncMock()
    monthly = plan()
    session = FakeSession(monthly, None)

    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False),
    )

    await subscription_handlers.buy_plan_chosen(
        cb,
        session,
        state,
        user(referrer_id=99),
    )

    text = cb.message.edit_text.await_args.args[0]
    assert "$19 (₽ в Lava Top)" in text
    assert "по курсу" not in text
    assert "Скидка по приглашению" in text
    assert "$15.20" in text
    assert "0 ₽" not in text
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.choosing_method)


@pytest.mark.asyncio
async def test_plan_purchase_lookup_requires_the_plan_to_be_active():
    session = FakeSession(None)

    assert await subscription_handlers._load_plan(session, 20) is None

    sql = str(session.queries[0].compile(compile_kwargs={"literal_binds": True}))
    assert "plans.id = 20" in sql
    assert "plans.is_active IS true" in sql


@pytest.mark.asyncio
async def test_stale_plan_keyboard_cannot_open_payment_methods():
    cb = callback("buy_plan:20")
    state = AsyncMock()

    await subscription_handlers.buy_plan_chosen(
        cb,
        FakeSession(None),
        state,
        user(),
    )

    cb.answer.assert_awaited_once_with("Тариф не найден", show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_deactivated_plan_cannot_resume_checkout(monkeypatch):
    cb = callback("pm:stripe:20")
    state = AsyncMock()
    state.get_data.return_value = {
        "plan_id": 20,
        "gift_recipient_id": None,
        "promo_code": None,
    }
    checkout = AsyncMock()
    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", checkout)

    await subscription_handlers.payment_method_chosen(
        cb,
        FakeSession(None),
        state,
        user(),
    )

    cb.answer.assert_awaited_once_with("Сессия истекла", show_alert=True)
    state.clear.assert_awaited_once_with()
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_subscription_can_display_an_inactive_historical_plan(monkeypatch):
    inactive_plan = plan(is_active=False)
    sub = active_subscription()
    session = FakeSession(inactive_plan)

    monkeypatch.setattr(
        subscription_handlers,
        "get_active_subscription",
        AsyncMock(return_value=sub),
    )

    loaded_sub, loaded_plan = await subscription_handlers._subscription_context(
        session,
        user(),
    )

    assert loaded_sub is sub
    assert loaded_plan is inactive_plan
    sql = str(session.queries[0].compile(compile_kwargs={"literal_binds": True}))
    assert "plans.id = 20" in sql
    where_clause = sql.split("WHERE", 1)[1]
    assert "plans.is_active" not in where_clause


@pytest.mark.asyncio
async def test_payment_callback_uses_callback_plan_token_not_stale_state(monkeypatch):
    cb = callback("pm:stripe:2")
    state = AsyncMock()
    state.get_data.return_value = {
        "plan_id": 1,
        "gift_recipient_id": None,
        "promo_code": "OLDPLAN",
    }
    six_month = plan(id=2, code="6m", name="6 месяцев", price_usd=Decimal("89"), duration_days=180)
    captured = []

    async def fake_checkout(_session, _user, chosen_plan, gift_recipient_id, *, is_gift=False, promo_code=None):
        captured.append((chosen_plan, gift_recipient_id, is_gift, promo_code))
        return SimpleNamespace(url="https://checkout.example.test/2")

    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", fake_checkout)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False),
    )

    await subscription_handlers.payment_method_chosen(
        cb,
        FakeSession(six_month),
        state,
        user(referrer_id=99),
    )

    assert captured == [(six_month, None, False, None)]
    state.update_data.assert_awaited_with(plan_id=2, gift_purchase=False, gift_recipient_id=None, promo_code=None)
    state.clear.assert_not_awaited()
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "back_to_methods:2" in [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_cancel_autorenew_confirm_dispatches_to_provider_and_keeps_access_copy(monkeypatch):
    """GK-377: the confirm step must reach Stripe, not just flip a local flag."""
    cb = callback("sub_cancel_confirm")
    state = AsyncMock()
    sub = active_subscription()
    calls = []

    async def fake_cancel(subscription_id):
        calls.append(subscription_id)

    monkeypatch.setattr(StripeProvider, "cancel_autorenew", fake_cancel)

    async def fake_active_subscription(_session, user_id):
        assert user_id == 10
        return sub

    monkeypatch.setattr(subscription_handlers, "get_active_subscription", fake_active_subscription)

    await subscription_handlers.subscription_cancel_confirm(
        cb,
        FakeSession(plan()),
        state,
        user(),
    )

    assert calls == ["sub_123"]
    assert sub.cancel_at_period_end is True
    support_message = cb.message.edit_text.await_args
    assert "Доступ сохраняется до <b>01.07.2026</b>" in support_message.args[0]
    markup = support_message.kwargs["reply_markup"]
    assert all("Возврат" not in text for text in button_texts(markup))


@pytest.mark.asyncio
async def test_cancel_autorenew_queues_manual_item_when_provider_cannot_confirm(monkeypatch):
    """When the provider call cannot be made, a human-actionable item is filed."""
    cb = callback("sub_cancel_confirm")
    state = AsyncMock()
    sub = active_subscription()
    session = FakeSession(plan(), None)

    async def fake_cancel(subscription_id):
        raise StripeCancellationError("Stripe autorenew cancellation is not enabled")

    monkeypatch.setattr(StripeProvider, "cancel_autorenew", fake_cancel)

    async def fake_active_subscription(_session, _user_id):
        return sub

    monkeypatch.setattr(subscription_handlers, "get_active_subscription", fake_active_subscription)

    await subscription_handlers.subscription_cancel_confirm(cb, session, state, user())

    assert len(session.added) == 1
    request = session.added[0]
    assert isinstance(request, SupportMessage)
    assert request.user_id == 10
    assert request.role == "user"
    assert "РУЧНАЯ ОТМЕНА" in request.content
    assert "provider_subscription_id=sub_123" in request.content
    # The member is not told the charge was stopped.
    assert "Автопродление отключено" not in cb.message.edit_text.await_args.args[0]
    assert sub.cancel_at_period_end is False


def test_usdt_awaiting_hash_keyboard_drops_redundant_method_buttons():
    markup = keyboards.usdt_awaiting_hash_keyboard("20")
    labels = button_texts(markup)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    # Only a single back-to-methods path remains; the full Stripe/Lava/USDT list
    # is intentionally not re-shown on the "send your tx hash" screen (GK-381 / C10).
    assert callbacks == ["back_to_methods:20"]
    assert all("Stripe" not in text and "USDT" not in text and "Lava" not in text for text in labels)
    assert all("Промокод" not in text for text in labels)


def test_usdt_renewal_keyboard_opens_normal_purchase_flow():
    markup = keyboards.usdt_renewal_keyboard()
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert [(button.text, button.callback_data) for button in buttons] == [
        ("Продлить подписку", "buy_start")
    ]


@pytest.mark.asyncio
async def test_buy_start_clears_stale_fsm_before_showing_plans():
    cb = callback("buy_start")
    state = AsyncMock()

    await subscription_handlers.buy_start_cb(cb, FakeSession([plan()]), state)

    state.clear.assert_awaited_once_with()
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.choosing_plan)
    assert "Выберите тариф" in cb.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_usdt_method_screen_states_one_time_and_uses_minimal_keyboard(monkeypatch):
    cb = callback("pm:usdt_trc20:20")
    state = AsyncMock()
    state.get_data.return_value = {"plan_id": 20, "gift_recipient_id": None, "promo_code": None}
    monthly = plan()

    async def fake_manual_checkout(
        _session, _user, chosen_plan, method, gift_recipient_id, *, is_gift=False, promo_code=None
    ):
        assert method == "usdt_trc20"
        return SimpleNamespace(
            payment_id=901,
            url=None,
            instructions="<b>USDT TRC20 — 1 месяц ($19)</b>\nАдрес: <code>addr</code>",
        )

    monkeypatch.setattr(subscription_handlers.ManualProvider, "create_checkout", fake_manual_checkout)
    monkeypatch.setattr(subscription_handlers, "settings", SimpleNamespace(enable_zelle=False))

    await subscription_handlers.payment_method_chosen(cb, FakeSession(monthly), state, user())

    text = cb.message.edit_text.await_args.args[0]
    # B06 / C10: deposit screen must say one-time, no auto-charge, manual renewal,
    # and keep the original provider instructions.
    assert "Адрес: <code>addr</code>" in text
    assert "разовый платёж" in text
    assert "автосписаний нет" in text
    assert "вручную" in text

    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == ["back_to_methods:20"]

    # FSM advances to await the tx hash; verification path is untouched.
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.awaiting_usdt_tx)
    state.update_data.assert_any_await(payment_id=901, usdt_network="TRC20")


@pytest.mark.asyncio
async def test_lava_checkout_falls_back_to_usd_when_rub_price_is_zero(monkeypatch):
    monkeypatch.setattr(
        lava_provider,
        "settings",
        SimpleNamespace(
            enable_lava_live_checkout=False,
            public_base_url="https://example.test",
            lava_api_key="",
        ),
    )
    session = FakeSession()

    result = await LavaProvider.create_checkout(session, user(), plan(price_rub=Decimal("0")))

    assert result.url == "https://example.test/dev/lava-stub?payment_id=901"
    payment = session.added[0]
    assert payment.amount == Decimal("19.00")
    assert payment.currency == "USD"


@pytest.mark.asyncio
async def test_live_lava_method_prompts_for_buyer_email_before_checkout(monkeypatch):
    cb = callback("pm:lava:20")
    state = AsyncMock()
    state.get_data.return_value = {
        "plan_id": 20,
        "gift_recipient_id": None,
        "promo_code": None,
    }
    checkout = AsyncMock()
    monkeypatch.setattr(subscription_handlers.LavaProvider, "create_checkout", checkout)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=True),
    )

    await subscription_handlers.payment_method_chosen(
        cb,
        FakeSession(plan()),
        state,
        user(),
    )

    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.awaiting_lava_email)
    assert "email" in cb.message.edit_text.await_args.args[0].lower()
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_lava_email_submission_creates_checkout_with_normalized_email(monkeypatch):
    message = SimpleNamespace(text=" Buyer@Example.COM ", answer=AsyncMock())
    state = AsyncMock()
    state.get_data.return_value = {
        "plan_id": 20,
        "gift_purchase": False,
        "gift_recipient_id": None,
        "promo_code": None,
    }
    captured = []

    async def fake_checkout(
        _session,
        _user,
        selected_plan,
        gift_recipient_id,
        *,
        is_gift=False,
        promo_code=None,
        buyer_email=None,
    ):
        captured.append(
            (selected_plan.id, gift_recipient_id, is_gift, promo_code, buyer_email)
        )
        return SimpleNamespace(
            payment_id=901,
            url="https://pay.lava.top/contract_1m",
            instructions=None,
        )

    monkeypatch.setattr(subscription_handlers.LavaProvider, "create_checkout", fake_checkout)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=True),
    )

    await subscription_handlers.lava_email_submitted(
        message,
        FakeSession(plan()),
        state,
        user(),
    )

    assert captured == [(20, None, False, None, "buyer@example.com")]
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.choosing_method)
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "https://pay.lava.top/contract_1m" in [
        button.url for row in markup.inline_keyboard for button in row if button.url
    ]


@pytest.mark.asyncio
async def test_lava_email_submission_rejects_invalid_email():
    message = SimpleNamespace(text="not-an-email", answer=AsyncMock())
    state = AsyncMock()

    await subscription_handlers.lava_email_submitted(
        message,
        FakeSession(),
        state,
        user(),
    )

    assert "email" in message.answer.await_args.args[0].lower()
    state.get_data.assert_not_awaited()
