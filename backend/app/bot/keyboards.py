from decimal import Decimal, InvalidOperation

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.db.models import Plan

# GK-413: layout matches Grant's approved button doc ("Описание кнопок в боте",
# 2026-07-09) exactly. Standalone 🤝 Пригласить друга / 💰 Партнёрам entries were
# dropped from the top level in favour of 🏆 Лидерборд; both flows stay reachable
# via /invite, /partner and the Cabinet → 💰 Партнёрам inline shortcut.
MAIN_MENU_LAYOUT: tuple[tuple[str, ...], ...] = (
    ("💎 Подписка", "👤 Кабинет"),
    ("🎬 Открыть архив", "🎁 Подарить"),
    ("🏆 Лидерборд", "💬 Поддержка"),
    ("ℹ️ О сообществе",),
)

#: GK-453: every label the reply keyboard can produce, as one set.
#:
#: A reply keyboard is permanent — it sits under the chat and a member can tap
#: it at any moment, including in the middle of an FSM flow. Several handlers
#: claim *all* text in their state (the support dialog, the promo/email/tx-hash
#: steps), so whichever of them the member happens to be inside will consume the
#: tap as free text and the button appears broken. That is exactly what Grant
#: reported for «🎁 Подарить» on 19.08: it was answered by the support ticket
#: acknowledgement instead of opening the gift flow.
#:
#: Those catch-alls filter this set out, so a menu tap always falls through to
#: the handler that owns it. Derived from the layout above so the two can never
#: drift: a button that is added to the keyboard is protected the same day.
MENU_BUTTON_TEXTS: frozenset[str] = frozenset(
    label for row in MAIN_MENU_LAYOUT for label in row
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in MAIN_MENU_LAYOUT],
        resize_keyboard=True,
    )


def _money_compact(value: object) -> str:
    try:
        amount = Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if amount == amount.quantize(Decimal("1")):
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def plan_price_label(plan: Plan, *, include_lava_hint: bool = False) -> str:
    usd = f"${_money_compact(plan.price_usd)}"
    try:
        rub = Decimal(str(getattr(plan, "price_rub", None) or "0"))
    except (InvalidOperation, TypeError, ValueError):
        rub = Decimal("0")
    if rub > 0:
        return f"{usd} / {_money_compact(rub)} ₽"
    if include_lava_hint:
        return f"{usd} (₽ в Lava Top)"
    return usd


def plans_keyboard(
    plans: list[Plan],
    for_gift: bool = False,
    *,
    back_callback_data: str = "sub_back",
) -> InlineKeyboardMarkup:
    prefix = "gift_plan" if for_gift else "buy_plan"
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.name} - {plan_price_label(p)}",
                callback_data=f"{prefix}:{p.id}",
            )
        ]
        for p in plans
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_method_keyboard(
    payment_token: str,
    *,
    enable_zelle: bool = False,
    applied_promo: str | None = None,
    allow_promo: bool = True,
    back_callback_data: str | None = "back_to_plans",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="💳 Карта (Stripe)", callback_data=f"pm:stripe:{payment_token}")],
        [InlineKeyboardButton(text="🇷🇺 Lava Top (карта РФ / СБП, ₽)", callback_data=f"pm:lava:{payment_token}")],
    ]
    # GK-120: Zelle is launch-hidden by default; demo/staging can re-enable
    # by setting ENABLE_ZELLE=true. Existing zelle payment rows stay readable.
    if enable_zelle:
        rows.append([InlineKeyboardButton(text="🏦 Zelle", callback_data=f"pm:zelle:{payment_token}")])
    rows.append([InlineKeyboardButton(text="₮ USDT TRC20", callback_data=f"pm:usdt_trc20:{payment_token}")])
    rows.append([InlineKeyboardButton(text="₮ USDT ERC20", callback_data=f"pm:usdt_erc20:{payment_token}")])
    if allow_promo:
        # GK-210: optional promo code. Label reflects whether one is already applied.
        promo_label = f"🎟 Промокод: {applied_promo}" if applied_promo else "🎟 Ввести промокод"
        rows.append([InlineKeyboardButton(text=promo_label, callback_data=f"promo:{payment_token}")])
    # Public offer agreement (Decision Log 2026-06-24): a single, method-agnostic
    # button that delivers the offer PDF from the bot. Shown on every payment
    # method so the terms are one tap away regardless of how the user pays.
    rows.append([InlineKeyboardButton(text="📄 Договор оферты", callback_data="offer_doc")])
    if back_callback_data:
        rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def usdt_awaiting_hash_keyboard(payment_token: str) -> InlineKeyboardMarkup:
    """Minimal keyboard for the "now send your USDT tx hash" step (GK-381 / C10).

    The full payment-method list is intentionally NOT re-shown here. Once the user
    has the deposit address and the FSM is waiting for a tx hash, re-listing
    Stripe/Lava/USDT buttons is a redundant dead end — the next expected action is
    pasting the hash as a plain message. Only a single path back to the method
    picker remains, so the user can still change their mind without being offered
    the same flow they just started.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к способам оплаты", callback_data=f"back_to_methods:{payment_token}")],
        ]
    )


def usdt_renewal_keyboard() -> InlineKeyboardMarkup:
    """Open the normal plan picker for a fresh one-time USDT renewal (GK-384)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продлить подписку", callback_data="buy_start")],
        ]
    )


def cabinet_keyboard(has_active_sub: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_active_sub:
        rows.append([InlineKeyboardButton(text="⚙️ Управление подпиской", callback_data="subscription_manage")])
        rows.append([InlineKeyboardButton(text="💰 Партнёрам", callback_data="my_ref_link")])
        rows.append([InlineKeyboardButton(text="🎁 Подарить подписку", callback_data="gift_start")])
    else:
        rows.append([InlineKeyboardButton(text="💎 Оформить подписку", callback_data="buy_start")])
    rows.append([InlineKeyboardButton(text="🏆 Лидерборд рефералов", callback_data="leaderboard")])
    rows.append([InlineKeyboardButton(text="💬 Задать вопрос", callback_data="support_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def open_url(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def checkout_keyboard(text: str, url: str, *, back_callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, url=url)],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback_data)],
        ]
    )


def subscription_management_keyboard(*, can_cancel_autorenew: bool, cancel_requested: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💎 Продлить или выбрать тариф", callback_data="buy_start")]]
    if can_cancel_autorenew:
        label = "✅ Запрос на отмену отправлен" if cancel_requested else "🚫 Отменить автопродление"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data="sub_cancel_already" if cancel_requested else "sub_cancel_start",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🎁 Подарить подписку", callback_data="gift_start")])
    rows.append([InlineKeyboardButton(text="💰 Партнёрам", callback_data="my_ref_link")])
    rows.append([InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_cancel_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить запрос на отмену", callback_data="sub_cancel_confirm")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="subscription_manage")],
        ]
    )
