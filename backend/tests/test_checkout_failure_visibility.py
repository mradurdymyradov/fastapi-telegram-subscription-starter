"""GK-421: a checkout that fails must say so, not spin forever.

Three of the four `create_checkout` call sites in `payment_method_chosen()`
had no error handling, and there was no dispatcher-level error handler — so an
exception escaped aiogram, the callback query was never answered, and Telegram
left the button spinning. That is exactly what the 28.07 Stripe incident looked
like to the buyer, and the report that came back was «кнопка не открывается».

What each test here pins down:

* every call site catches, tells the member, restores the payment-method
  keyboard and **answers the callback** (the spinner is the visible symptom);
* the dispatcher-level handler does the same for anything nobody anticipated;
* the Stripe price-drift guard refuses rather than charging an amount the
  member was never shown — GK-422's defect, which nothing would have caught;
* every screen that hands out a payment link carries Grant's help line, for the
  failure that never reaches us at all — GK-424, below.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import errors as bot_errors
from app.bot.handlers import subscription as subscription_handlers
from app.payments.stripe_provider import StripePriceMismatch


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

    async def execute(self, _query):
        if not self.values:
            raise AssertionError("FakeSession.execute called without queued result")
        return Result(self.values.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 901


def plan(**overrides):
    data = {
        "id": 20,
        "code": "1m",
        "name": "1 месяц",
        "price_rub": Decimal("1500"),
        "price_usd": Decimal("19.00"),
        "duration_days": 30,
        "is_active": True,
        "sort_order": 1,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def user(**overrides):
    data = {"id": 10, "tg_id": 10010, "username": "member", "referrer_id": None}
    data.update(overrides)
    return SimpleNamespace(**data)


def callback(data: str):
    return SimpleNamespace(data=data, message=AsyncMock(), answer=AsyncMock())


def buy_state():
    state = AsyncMock()
    state.get_data.return_value = {
        "plan_id": 20,
        "gift_recipient_id": None,
        "promo_code": None,
    }
    return state


@pytest.fixture(autouse=True)
def _silence_ops_alerts(monkeypatch):
    """Alerts are asserted where they matter; elsewhere they must not fire."""
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(subscription_handlers, "send_ops_alert", sent)
    monkeypatch.setattr(bot_errors, "send_ops_alert", sent)
    return sent


def method_keyboard_callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# --------------------------------------------------------------------------
# the three previously-unguarded call sites
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("callback_data", "provider_attr", "exception"),
    [
        # Stripe: the literal 28.07 shape — a stale customer id makes the
        # Stripe SDK raise before any URL exists.
        ("pm:stripe:20", "StripeProvider", RuntimeError("No such customer: cus_dead")),
        # Stripe: a misconfigured Price raises ValueError from _price_id_for_plan.
        ("pm:stripe:20", "StripeProvider", ValueError("Stripe Price ID is not configured")),
        # Stripe: price drift now fails closed rather than overcharging.
        ("pm:stripe:20", "StripeProvider", StripePriceMismatch("charges 89 but the plan shows 79")),
        # Lava non-live fallback — reached whenever ENABLE_LAVA_LIVE_CHECKOUT is false.
        ("pm:lava:20", "LavaProvider", RuntimeError("connection reset")),
        # Manual/USDT.
        ("pm:usdt_trc20:20", "ManualProvider", RuntimeError("wallet address missing")),
    ],
)
@pytest.mark.asyncio
async def test_checkout_failure_is_visible_and_answers_the_callback(
    monkeypatch, callback_data, provider_attr, exception
):
    cb = callback(callback_data)
    state = buy_state()

    async def boom(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(
        getattr(subscription_handlers, provider_attr), "create_checkout", boom, raising=False
    )
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=False),
    )

    await subscription_handlers.payment_method_chosen(cb, FakeSession(plan()), state, user())

    # 1. the member is told something, in language they can act on
    cb.message.edit_text.assert_awaited()
    text = cb.message.edit_text.await_args.args[0]
    assert text.strip()
    assert "не списан" in text.lower()

    # 2. the payment-method keyboard comes back, so there is a way forward
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = method_keyboard_callbacks(markup)
    assert any(c and c.startswith("pm:") for c in callbacks)

    # 3. the spinner stops — the whole point of the task
    cb.answer.assert_awaited()

    # 4. the FSM is left somewhere the member can retry from
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.choosing_method)


@pytest.mark.asyncio
async def test_checkout_failure_raises_an_ops_alert(monkeypatch, _silence_ops_alerts):
    cb = callback("pm:stripe:20")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", boom)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=False),
    )

    await subscription_handlers.payment_method_chosen(
        cb, FakeSession(plan()), buy_state(), user()
    )

    _silence_ops_alerts.assert_awaited()
    body = _silence_ops_alerts.await_args.args[0]
    assert "stripe" in body
    assert _silence_ops_alerts.await_args.kwargs["severity"] == "error"
    # Rate-limited, or one broken provider becomes one alert per buyer.
    assert _silence_ops_alerts.await_args.kwargs["rate_limit_seconds"] > 0


@pytest.mark.asyncio
async def test_happy_path_is_unchanged(monkeypatch):
    cb = callback("pm:stripe:20")
    state = buy_state()

    async def ok(*_args, **_kwargs):
        return SimpleNamespace(payment_id=901, url="https://checkout.example.test/x", instructions=None)

    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", ok)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=False),
    )

    await subscription_handlers.payment_method_chosen(cb, FakeSession(plan()), state, user())

    text = cb.message.edit_text.await_args.args[0]
    assert "Создан счёт" in text
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "back_to_methods:20" in method_keyboard_callbacks(markup)
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_error_message_still_reaches_the_member_when_edit_fails(monkeypatch):
    """A stale message cannot be edited; the member must still hear something."""
    cb = callback("pm:stripe:20")
    cb.message.edit_text = AsyncMock(side_effect=RuntimeError("message is not modified"))

    async def boom(*_args, **_kwargs):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", boom)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=False),
    )

    await subscription_handlers.payment_method_chosen(
        cb, FakeSession(plan()), buy_state(), user()
    )

    cb.message.answer.assert_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_lava_email_branch_catches_more_than_the_two_lava_errors(monkeypatch):
    """It handled LavaCheckoutUnavailable and LavaAPIError — and nothing else."""
    message = AsyncMock()
    message.text = "buyer@example.com"
    state = buy_state()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(subscription_handlers.LavaProvider, "create_checkout", boom)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=True),
    )

    await subscription_handlers.lava_email_submitted(
        message, FakeSession(plan()), state, user()
    )

    text = message.answer.await_args.args[0]
    assert "не списан" in text.lower()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert any(c and c.startswith("pm:") for c in method_keyboard_callbacks(markup))
    state.set_state.assert_awaited_with(subscription_handlers.BuyFlow.choosing_method)


# --------------------------------------------------------------------------
# GK-424: the payment page that opens but never loads
# --------------------------------------------------------------------------
#
# GK-421 above covers the failures we can see — an exception on our side, caught
# and reported. This covers the one we cannot: the Lava page loads, the button is
# there, and one third-party script fails silently, leaving «Загрузка...» forever.
# Nothing reaches our logs, so no handler can react. Grant's answer (16.08) is a
# standing line pointing at a human, and its exact wording is his.


GRANT_PAYMENT_HELP_LINE = (
    "Если страница оплаты не открывается или зависает, напишите @GKcurators, поможем оплатить."
)


def test_the_help_line_is_grants_text_character_for_character():
    """Grant's copy is applied verbatim — never re-phrased or re-punctuated.

    Pinned as a literal rather than compared against itself: a later edit that
    "tidies" the comma or drops the handle has to fail here, where the reason is
    written down, instead of reaching a member.
    """
    assert subscription_handlers.PAYMENT_HELP_LINE == GRANT_PAYMENT_HELP_LINE


def test_the_help_line_does_not_tell_anyone_to_change_browser():
    """Grant refused «откройте в Chrome» twice — 23.07, and again on 16.08.

    It is the obvious thing to write against this symptom and it is the one thing
    he does not want in the bot, so it gets a test rather than a memory.
    """
    lowered = subscription_handlers.PAYMENT_HELP_LINE.lower()
    for refused in ("chrome", "safari", "браузер"):
        assert refused not in lowered


@pytest.mark.asyncio
async def test_stripe_checkout_screen_carries_the_help_line(monkeypatch):
    cb = callback("pm:stripe:20")

    async def ok(*_args, **_kwargs):
        return SimpleNamespace(payment_id=901, url="https://checkout.example.test/x", instructions=None)

    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", ok)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=False),
    )

    await subscription_handlers.payment_method_chosen(cb, FakeSession(plan()), buy_state(), user())

    assert GRANT_PAYMENT_HELP_LINE in cb.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_lava_checkout_screen_carries_the_help_line(monkeypatch):
    """The screen the symptom was actually reported against."""
    cb = callback("pm:lava:20")

    async def ok(*_args, **_kwargs):
        return SimpleNamespace(payment_id=902, url="https://lava.example.test/i", instructions=None)

    monkeypatch.setattr(subscription_handlers.LavaProvider, "create_checkout", ok)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=False),
    )

    await subscription_handlers.payment_method_chosen(cb, FakeSession(plan()), buy_state(), user())

    assert GRANT_PAYMENT_HELP_LINE in cb.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_live_lava_email_branch_carries_the_help_line(monkeypatch):
    """The live RUB route — the one most members will take, and the one that hangs."""
    message = AsyncMock()
    message.text = "buyer@example.com"

    async def ok(*_args, **_kwargs):
        return SimpleNamespace(payment_id=903, url="https://lava.example.test/live", instructions=None)

    monkeypatch.setattr(subscription_handlers.LavaProvider, "create_checkout", ok)
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=True),
    )

    await subscription_handlers.lava_email_submitted(
        message, FakeSession(plan()), buy_state(), user()
    )

    assert GRANT_PAYMENT_HELP_LINE in message.answer.await_args.args[0]


# --------------------------------------------------------------------------
# the dispatcher-level handler
# --------------------------------------------------------------------------


def error_event(*, with_callback: bool):
    message = AsyncMock()
    if with_callback:
        cb = SimpleNamespace(message=message, answer=AsyncMock())
        update = SimpleNamespace(
            update_id=77,
            callback_query=cb,
            message=None,
            event_from_user=SimpleNamespace(id=10010),
        )
    else:
        cb = None
        update = SimpleNamespace(
            update_id=78,
            callback_query=None,
            message=message,
            event_from_user=SimpleNamespace(id=10010),
        )
    event = SimpleNamespace(update=update, exception=RuntimeError("nobody saw this coming"))
    return event, message, cb


@pytest.mark.asyncio
async def test_global_handler_answers_callback_and_tells_the_member():
    event, message, cb = error_event(with_callback=True)

    handled = await bot_errors.handle_bot_error(event)

    assert handled is True
    cb.answer.assert_awaited()
    message.answer.assert_awaited_once()
    assert "не списано" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_global_handler_covers_plain_messages_too():
    event, message, _ = error_event(with_callback=False)

    await bot_errors.handle_bot_error(event)

    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_handler_alerts_ops_rate_limited_by_exception_type(_silence_ops_alerts):
    event, _, _ = error_event(with_callback=True)

    await bot_errors.handle_bot_error(event)

    _silence_ops_alerts.assert_awaited()
    assert _silence_ops_alerts.await_args.kwargs["key"] == "bot_unhandled:RuntimeError"
    assert _silence_ops_alerts.await_args.kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_global_handler_never_raises_even_when_everything_fails(monkeypatch):
    """An error handler that raises is worse than no error handler."""
    event, message, cb = error_event(with_callback=True)
    cb.answer = AsyncMock(side_effect=RuntimeError("telegram unreachable"))
    message.answer = AsyncMock(side_effect=RuntimeError("telegram unreachable"))
    monkeypatch.setattr(
        bot_errors, "send_ops_alert", AsyncMock(side_effect=RuntimeError("alerting down"))
    )

    assert await bot_errors.handle_bot_error(event) is True


@pytest.mark.asyncio
async def test_global_handler_survives_an_update_it_cannot_read():
    event = SimpleNamespace(update=None, exception=ValueError("no update at all"))

    assert await bot_errors.handle_bot_error(event) is True


def test_error_handler_is_registered_on_the_dispatcher():
    from aiogram import Dispatcher

    dp = Dispatcher()
    bot_errors.register_error_handler(dp)

    assert dp.errors.handlers, "no error handler registered on the dispatcher"
    assert any(
        h.callback is bot_errors.handle_bot_error for h in dp.errors.handlers
    ), "the registered error handler is not handle_bot_error"


def test_the_dispatcher_the_bot_actually_runs_has_the_handler(monkeypatch):
    """The one that matters: a handler nobody wired into `main` is nothing.

    Asserting `register_error_handler()` works is not the same as asserting the
    running bot calls it, and it is the second one that was missing.
    """
    from aiogram import Router
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot import main as bot_main

    monkeypatch.setattr(bot_main, "_build_storage", lambda: MemoryStorage())
    # The real handler routers are module-level singletons and another test
    # module already attached them to its own dispatcher; a second attach
    # raises. Swap in an empty router — the line under test is the one after.
    monkeypatch.setattr(bot_main, "setup_handlers", lambda: Router(name="empty"))

    dp = bot_main._build_dispatcher()

    assert any(
        h.callback is bot_errors.handle_bot_error for h in dp.errors.handlers
    ), "the bot's own dispatcher has no error handler — unhandled errors stay invisible"
