"""GK-486: accounts the bot will not sell to, however hard they press.

Grant asked on 23.08 for Owner to be given the real bot on both of his
accounts. Doing that put two accounts past the pre-launch hold and in front of
live Stripe and Lava checkouts, and the only thing standing between him and a
charge on his own card was a sentence in a Telegram message asking him not to
press «💎 Подписка». His account already had five payment attempts on record
from June — `pay#9`, `#33`, `#35` pending and `#10`, `#32` failed — so "he
won't press it" was not a prediction the data supported.

The fix is that the button stops working for him, not that he is asked nicely.

Two layers, on purpose:

* **This router**, registered immediately before `subscription.router`, claims
  every entry into the buying flow for the listed ids and answers with a plain
  explanation. This is the layer the member actually meets — they never reach
  the plan list, so they never see a price or a payment method.
* **`Settings.charges_blocked_for` again inside the two handlers that create
  checkouts.** That is the layer that matters. Router ordering is a property of
  a list somebody maintains; GK-453 is the recorded case of exactly that list
  being wrong for months. If a future buying path is added and nobody thinks of
  this file, the entry stays open — but the charge still cannot be created.

Registered unconditionally, but inert unless `NO_CHARGE_TG_IDS` is set: with it
empty the filter is False for everybody and the router claims nothing, so a
deployment that never configures it behaves exactly as it did before.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.subscription import BuyFlow
from app.config import get_settings

logger = logging.getLogger(__name__)

router = Router(name="no_charge")

#: What a blocked account is told. Deliberately not an error and not an
#: apology: for the people on this list the answer is genuinely good news —
#: they already have access and were never meant to buy anything.
#:
#: It does not say "ask an admin to enable it", because the person reading it
#: is the admin. It does not name the setting either — a member-facing string
#: that names an environment variable is a support ticket waiting to happen.
NO_CHARGE_MESSAGE = (
    "💎 Оплата для этого аккаунта отключена.\n\n"
    "Он отмечен как служебный: доступ у вас уже есть и покупать ничего не нужно. "
    "Это сделано специально, чтобы случайное нажатие не списало деньги с карты.\n\n"
    "Всё остальное в боте работает как обычно."
)


def _blocked(event: Message | CallbackQuery) -> bool:
    """True when this event comes from an account that must not be charged.

    `get_settings()` is read here rather than captured at import time so the
    predicate follows a reconfigured `Settings` in tests; in production the
    call is cached and this costs nothing.

    The `.strip()` guard is what keeps the router inert when the feature is
    unconfigured. Without it `charges_blocked_for(None)` — an event Telegram
    handed us with no `from_user` — would be True even with an empty list, and
    this router would start claiming unattributable callbacks away from
    `subscription.py` on every deployment, configured or not. Fail-closed is
    right for "may this person be charged"; it is not right for "does this
    feature apply at all".
    """
    settings = get_settings()
    if not (settings.no_charge_tg_ids or "").strip():
        return False
    return settings.charges_blocked_for(
        getattr(getattr(event, "from_user", None), "id", None)
    )


async def _refuse_message(message: Message) -> None:
    logger.info(
        "GK-486: refused a buying entry point for no-charge account %s",
        getattr(getattr(message, "from_user", None), "id", None),
    )
    await message.answer(NO_CHARGE_MESSAGE)


async def _refuse_callback(cb: CallbackQuery) -> None:
    logger.info(
        "GK-486: refused a buying callback for no-charge account %s",
        getattr(getattr(cb, "from_user", None), "id", None),
    )
    # `show_alert` rather than a toast: a toast is easy to miss, and the point
    # is that the person understands why the button did nothing.
    await cb.answer("Оплата для этого аккаунта отключена", show_alert=True)
    if cb.message is not None:
        await cb.message.answer(NO_CHARGE_MESSAGE)


def _register(target: Router, blocked: Callable[[Message | CallbackQuery], bool]) -> None:
    """Every way into the buying flow, in one place so it can be read as a set.

    Kept as an explicit enumeration rather than a prefix match on the callback
    data, because `subscription.py` also owns callbacks that a blocked account
    *should* keep — `subscription_manage`, `sub_cancel_start`, `offer_doc`.
    Grant is on this list and the management screen is the one he opens.
    """
    # Entry points.
    target.message.register(_refuse_message, blocked, Command("subscribe"))
    target.message.register(_refuse_message, blocked, F.text == "💎 Подписка")
    target.callback_query.register(_refuse_callback, blocked, F.data == "buy_start")
    # Plan chosen, method chosen, promo, and the two "go back" steps that
    # re-enter the flow. `pm:` is the one that spends money.
    for prefix in ("buy_plan:", "pm:", "promo:", "back_to_methods:"):
        target.callback_query.register(
            _refuse_callback, blocked, F.data.startswith(prefix)
        )
    target.callback_query.register(_refuse_callback, blocked, F.data == "back_to_plans")
    # Mid-flow states. A blocked account should not be able to finish a flow it
    # started before it was listed — `awaiting_lava_email` is the *second* place
    # in this codebase that calls `create_checkout`.
    for state in (
        BuyFlow.awaiting_lava_email,
        BuyFlow.awaiting_promo,
        BuyFlow.awaiting_usdt_tx,
    ):
        target.message.register(_refuse_message, blocked, state)


_register(router, _blocked)
