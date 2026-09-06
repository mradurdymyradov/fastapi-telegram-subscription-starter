import html
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    MENU_BUTTON_TEXTS,
    checkout_keyboard,
    payment_method_keyboard,
    plan_price_label,
    plans_keyboard,
    subscription_cancel_confirm_keyboard,
    subscription_management_keyboard,
    usdt_awaiting_hash_keyboard,
)
from app.config import get_settings
from app.db.models import Payment, Plan, Subscription, User
from app.observability import send_ops_alert
from app.payments import LavaProvider, ManualProvider, StripeProvider
from app.payments.lava_provider import LavaAPIError, LavaCheckoutUnavailable
from app.payments.usdt_verifier import extract_tx_hash, verify_and_apply_usdt_payment
from app.services.billing_notifications import build_payment_succeeded_message
from app.services.gifts import (
    build_paid_gift_activation_message,
    ensure_paid_gift_activation,
    is_launch_gift_plan,
)
from app.services.promo import normalize_code, validate_promo_code
from app.services.referral import referral_discount_for_checkout
from app.services.subscription import (
    get_active_subscription,
    is_comp_access,
    subscription_access_ends_at,
)
from app.services.subscription_cancellation import (
    CANCEL_MANUAL_REQUIRED,
    CANCEL_PROVIDER_CONFIRMED,
    effective_cancel_state,
    has_cancellation_on_record,
    request_autorenew_cancellation,
)

router = Router(name="subscription")
settings = get_settings()
logger = logging.getLogger(__name__)
_EMAIL_ADAPTER = TypeAdapter(EmailStr)

# Public offer agreement (Decision Log 2026-06-24). Shipped as a static asset
# inside the bot image (Dockerfile copies app/ → /app/app) and handed out from
# the "📄 Договор оферты" button on the payment-method screen, identically for
# every payment method.
OFFER_DOCUMENT_PATH = Path(__file__).resolve().parent.parent / "assets" / "offer_agreement.pdf"
OFFER_DOCUMENT_FILENAME = "Договор-оферты.pdf"
OFFER_DOCUMENT_CAPTION = "📄 Договор публичной оферты"

# GK-381 (C10 / B06): the launch crypto flow is a one-time payment, not a
# recurring subscription. The deposit screen must say so plainly and set honest
# expectations about confirmation (auto-verify first, curator fallback), so users
# don't read "доступ откроется" as "this renews itself automatically".
USDT_ONE_TIME_NOTE = (
    "ℹ️ Это <b>разовый платёж</b>: автосписаний нет. "
    "Когда оплаченный период закончится, продление нужно запустить вручную — "
    "снова через «💎 Подписка» тем же способом.\n\n"
    "После того как пришлёте хэш, мы попробуем подтвердить платёж автоматически; "
    "если потребуется, куратор подтвердит вручную, обычно в течение часа."
)

# GK-424: Grant's approved line for a payment page that will not open, 2026-08-16.
# «Строку про браузер в бот не ставим, "откройте в Chrome" людям не пишем. Но
# короткую строку с контактом кураторов на случай проблем с оплатой оставь.»
#
# Verbatim, and hardcoded rather than interpolated from `settings.support_contact`
# for the same reason `HOLD_MESSAGE` hardcodes it: this is client-approved copy, and
# a settings value can drift into rendering a handle Grant never approved.
#
# It is a standing line on every screen that hands out a payment link, not a
# reaction to a detected failure — the failure mode it answers (GK-424: one
# third-party script failing silently, «Загрузка...» forever) is invisible to us.
# We never learn that the page did not open; only the member does.
PAYMENT_HELP_LINE = (
    "Если страница оплаты не открывается или зависает, напишите @GKcurators, поможем оплатить."
)

_PROMO_ERRORS = {
    "not_found": "Промокод не найден.",
    "inactive": "Промокод отключён.",
    "not_yet_active": "Промокод ещё не активен.",
    "expired": "Срок действия промокода истёк.",
    "plan_not_eligible": "Промокод не действует для выбранного тарифа.",
    "exhausted": "Лимит использований промокода исчерпан.",
    "already_redeemed": "Вы уже использовали этот промокод.",
    "gift_not_eligible": "Промокод нельзя применить к подарочной подписке.",
}


class BuyFlow(StatesGroup):
    choosing_plan = State()
    choosing_method = State()
    awaiting_promo = State()
    awaiting_lava_email = State()
    awaiting_usdt_tx = State()


def _promo_error_message(status: str) -> str:
    return _PROMO_ERRORS.get(status, "Промокод недействителен.")


def _normalize_buyer_email(value: str | None) -> str | None:
    try:
        return str(_EMAIL_ADAPTER.validate_python((value or "").strip())).lower()
    except ValidationError:
        return None


async def _list_plans(session: AsyncSession) -> list[Plan]:
    res = await session.execute(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order))
    return list(res.scalars().all())


async def _load_plan(session: AsyncSession, plan_id: int | None) -> Plan | None:
    if plan_id is None:
        return None
    return (
        await session.execute(
            select(Plan).where(
                Plan.id == plan_id,
                Plan.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _load_plan_including_inactive(
    session: AsyncSession,
    plan_id: int | None,
) -> Plan | None:
    """Load historical plan metadata without making the plan purchasable."""
    if plan_id is None:
        return None
    return (await session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()


def _callback_id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data.split(":", 1)[1]
    if not raw.isdigit():
        return None
    return int(raw)


def _format_usd(value: object) -> str:
    try:
        amount = Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return f"${html.escape(str(value))}"
    if amount == amount.quantize(Decimal("1")):
        return f"${int(amount)}"
    return f"${amount:.2f}"


def _format_date(value: object) -> str:
    if value is None:
        return "даты окончания оплаченного периода"
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return html.escape(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%d.%m.%Y")


def _provider_label(provider: str | None) -> str:
    labels = {
        "stripe": "Stripe",
        "lava": "Lava Top",
        "usdt": "USDT",
        "manual": "ручная оплата",
        "zelle": "Zelle",
        "gift": "подарок",
    }
    return labels.get(str(provider or "").lower(), html.escape(provider or "подписка"))


def _can_cancel_autorenew(sub: Subscription | None) -> bool:
    provider = str(getattr(sub, "provider", None) or getattr(sub, "source", "") or "").lower()
    return bool(provider in {"stripe", "lava"} or getattr(sub, "provider_subscription_id", None))


async def _checkout_failed(
    cb: CallbackQuery,
    state: FSMContext,
    plan: Plan,
    *,
    method: str,
    user: User,
    promo_code: str | None,
    is_gift: bool,
    back_to_plans_callback: str,
    text: str,
) -> None:
    """Tell the member the truth and give them somewhere to go (GK-421).

    Three of the four `create_checkout` call sites had no error handling at
    all. When one raised, the exception escaped the dispatcher, `cb.answer()`
    was never called, and Telegram left the button spinning forever — which is
    precisely what Grant saw on 28.07 and reported as «кнопка не открывается».
    From the member's side a silent failure and a broken product are the same
    thing, and at launch it is a lost sale nobody hears about.

    Everything here is best-effort: this is the error path, so it must not be
    able to raise a second time.
    """
    logger.exception(
        "checkout creation failed method=%s user_id=%s plan_id=%s",
        method,
        getattr(user, "id", None),
        getattr(plan, "id", None),
    )

    try:
        await state.set_state(BuyFlow.choosing_method)
    except Exception:  # noqa: BLE001 — never let bookkeeping hide the message
        logger.warning("could not reset FSM state after checkout failure", exc_info=True)

    keyboard = payment_method_keyboard(
        str(plan.id),
        enable_zelle=settings.enable_zelle,
        applied_promo=promo_code,
        allow_promo=not is_gift,
        back_callback_data=back_to_plans_callback,
    )
    try:
        await cb.message.edit_text(text, reply_markup=keyboard)
    except Exception:  # noqa: BLE001 — e.g. "message is not modified"
        logger.warning("could not edit message after checkout failure", exc_info=True)
        try:
            await cb.message.answer(text, reply_markup=keyboard)
        except Exception:
            logger.warning("could not send fallback message either", exc_info=True)

    # Always answer, or the spinner hangs regardless of what the text said.
    try:
        await cb.answer()
    except Exception:
        logger.warning("could not answer callback after checkout failure", exc_info=True)

    try:
        await send_ops_alert(
            # GK-451: neither the wrapper nor the escape. `send_ops_alert`
            # escapes the body itself, so `html.escape` here escaped twice —
            # a method containing "&" arrived as "&amp;".
            f"Не удалось создать счёт: {method}\n"
            f"user_id={getattr(user, 'id', None)} plan_id={getattr(plan, 'id', None)}\n"
            "Пользователю показано сообщение об ошибке и предложены другие способы оплаты.",
            # Rate-limited by method: one broken provider must not produce one
            # alert per buyer.
            key=f"checkout_failed:{method}",
            rate_limit_seconds=300,
            severity="error",
        )
    except Exception:  # noqa: BLE001 — alerting is not allowed to break the reply
        logger.warning("could not send ops alert for checkout failure", exc_info=True)


async def _referral_discount_line(
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    is_gift: bool,
    promo_code: str | None,
) -> str | None:
    if promo_code or is_gift or not getattr(user, "referrer_id", None):
        return None
    discount = await referral_discount_for_checkout(
        session,
        user,
        plan,
        plan.price_usd,
        is_gift=is_gift,
    )
    if not discount.applied:
        return None
    return (
        "Скидка по приглашению: "
        f"<b>{_format_usd(discount.amount)}</b> за первый месяц "
        f"вместо <s>{_format_usd(plan.price_usd)}</s>."
    )


async def _payment_method_text(
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    is_gift: bool = False,
    promo_code: str | None = None,
) -> str:
    lines = [
        f"<b>{html.escape(plan.name)}</b>",
        f"{plan_price_label(plan, include_lava_hint=True)} • {int(plan.duration_days)} дней",
    ]
    if promo_code:
        lines.append(f"Промокод: <b>{html.escape(promo_code)}</b>")
    referral_line = await _referral_discount_line(
        session,
        user,
        plan,
        is_gift=is_gift,
        promo_code=promo_code,
    )
    if referral_line:
        lines.append(referral_line)
    lines.extend(["", "Выберите способ оплаты:"])
    return "\n".join(lines)


async def _show_payment_methods(
    cb: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    user: User,
    plan: Plan,
    *,
    gift_recipient_id: int | None = None,
    is_gift: bool = False,
    promo_code: str | None = None,
):
    is_gift = is_gift or gift_recipient_id is not None
    back_to_plans_callback = "gift_back_to_plans" if is_gift else "back_to_plans"
    await state.update_data(
        plan_id=plan.id,
        gift_purchase=is_gift,
        gift_recipient_id=gift_recipient_id,
        promo_code=None if is_gift else promo_code,
    )
    await state.set_state(BuyFlow.choosing_method)
    await cb.message.edit_text(
        await _payment_method_text(
            session,
            user,
            plan,
            is_gift=is_gift,
            promo_code=None if is_gift else promo_code,
        ),
        reply_markup=payment_method_keyboard(
            str(plan.id),
            enable_zelle=settings.enable_zelle,
            applied_promo=None if is_gift else promo_code,
            allow_promo=not is_gift,
            back_callback_data=back_to_plans_callback,
        ),
    )


async def _show_plan_selection_message(message: Message, session: AsyncSession, state: FSMContext) -> None:
    plans = await _list_plans(session)
    if not plans:
        await message.answer("Тарифы временно недоступны. Загляните позже.")
        return
    await state.set_state(BuyFlow.choosing_plan)
    await message.answer(
        "<b>Выберите тариф:</b>\n\nЧем длиннее срок — тем выгоднее месячная цена.",
        reply_markup=plans_keyboard(plans),
    )


async def _show_plan_selection_callback(cb: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    plans = await _list_plans(session)
    if not plans:
        await cb.message.edit_text("Тарифы временно недоступны. Загляните позже.")
        await cb.answer()
        return
    await state.set_state(BuyFlow.choosing_plan)
    await cb.message.edit_text(
        "<b>Выберите тариф:</b>\n\nЧем длиннее срок — тем выгоднее месячная цена.",
        reply_markup=plans_keyboard(plans),
    )
    await cb.answer()


async def _subscription_context(
    session: AsyncSession,
    user: User,
) -> tuple[Subscription | None, Plan | None]:
    sub = await get_active_subscription(session, user.id)
    plan = (
        await _load_plan_including_inactive(session, getattr(sub, "plan_id", None))
        if sub
        else None
    )
    return sub, plan


def _subscription_manage_text(sub: Subscription, plan: Plan | None) -> str:
    access_until = _format_date(subscription_access_ends_at(sub))
    provider = _provider_label(getattr(sub, "provider", None) or getattr(sub, "source", None))
    plan_name = html.escape(getattr(plan, "name", None) or "текущий тариф")
    # GK-483: a comp row's `expires_at` is a date nothing acts on any more, so
    # printing it tells the member their access ends on a day it does not. Grant
    # is one of the people who will be flagged and this is the screen he opens.
    if is_comp_access(sub):
        return "\n".join(
            [
                "<b>Управление подпиской</b>",
                f"Тариф: <b>{plan_name}</b>",
                "Доступ: <b>бессрочный</b> — командный доступ, без оплаты.",
            ]
        )
    lines = [
        "<b>Управление подпиской</b>",
        f"Тариф: <b>{plan_name}</b>",
        f"Оплаченный доступ до: <b>{access_until}</b>",
        f"Оплата: <b>{provider}</b>",
    ]
    state = effective_cancel_state(sub)
    if state == CANCEL_PROVIDER_CONFIRMED:
        lines.append("Автопродление отключено. Списаний больше не будет.")
    elif state == CANCEL_MANUAL_REQUIRED:
        lines.append(
            "Запрос на отмену автопродления принят, но отключение у платёжного "
            "провайдера ещё не подтверждено — им занимается администратор."
        )
    elif state is not None:
        lines.append("Запрос на отмену автопродления зафиксирован.")
    elif _can_cancel_autorenew(sub):
        lines.append("Автопродление можно отменить здесь. Доступ останется до оплаченной даты.")
    else:
        lines.append("У этой подписки нет автопродления; доступ закончится в оплаченную дату.")
    return "\n".join(lines)


async def _render_subscription_management(
    cb: CallbackQuery,
    session: AsyncSession,
    user: User,
    state: FSMContext,
) -> None:
    await state.clear()
    sub, plan = await _subscription_context(session, user)
    if sub is None:
        await _show_plan_selection_callback(cb, session, state)
        return
    await cb.message.edit_text(
        _subscription_manage_text(sub, plan),
        reply_markup=subscription_management_keyboard(
            can_cancel_autorenew=_can_cancel_autorenew(sub),
            cancel_requested=has_cancellation_on_record(sub),
        ),
    )
    await cb.answer()


@router.message(Command("subscribe"))
@router.message(F.text == "💎 Подписка")
async def subscribe_entry(message: Message, session: AsyncSession, state: FSMContext, user: User):
    sub, plan = await _subscription_context(session, user)
    if sub is not None:
        await state.clear()
        await message.answer(
            _subscription_manage_text(sub, plan),
            reply_markup=subscription_management_keyboard(
                can_cancel_autorenew=_can_cancel_autorenew(sub),
                cancel_requested=has_cancellation_on_record(sub),
            ),
        )
        return
    await _show_plan_selection_message(message, session, state)


@router.callback_query(F.data.startswith("buy_plan:"))
async def buy_plan_chosen(cb: CallbackQuery, session: AsyncSession, state: FSMContext, user: User):
    plan_id = _callback_id(cb.data, "buy_plan:")
    plan = await _load_plan(session, plan_id)
    if not plan:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    await _show_payment_methods(
        cb,
        session,
        state,
        user,
        plan,
        gift_recipient_id=None,
        promo_code=None,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("pm:"))
async def payment_method_chosen(cb: CallbackQuery, session: AsyncSession, state: FSMContext, user: User):
    # GK-486: the point of no return — every provider branch below this line
    # calls `create_checkout`. `no_charge.router` normally claims `pm:` before it
    # ever gets here, so reaching this check at all means the routing changed.
    # That is exactly why the guard is duplicated rather than left to ordering.
    #
    # `get_settings()` and not the module-level `settings`, on purpose: about ten
    # test modules replace that attribute with a `SimpleNamespace` carrying only
    # the two or three flags they care about. A guard reading it would vanish
    # under any of those stubs — silently, and in exactly the tests that drive
    # this handler. A money guard must not be removable by an unrelated stub.
    if get_settings().charges_blocked_for(user.tg_id):
        await state.clear()
        await cb.answer("Оплата для этого аккаунта отключена", show_alert=True)
        return
    _, method, token = cb.data.split(":", 2)
    data = await state.get_data()
    callback_plan_id = int(token) if token.isdigit() else None
    plan_id = callback_plan_id or data.get("plan_id")
    state_plan_id = data.get("plan_id")
    gift_recipient_id = data.get("gift_recipient_id")
    is_gift = bool(data.get("gift_purchase") or gift_recipient_id is not None)
    promo_code = None if is_gift else data.get("promo_code") if state_plan_id == plan_id else None
    plan = await _load_plan(session, int(plan_id) if plan_id is not None else None)
    if not plan:
        await cb.answer("Сессия истекла", show_alert=True)
        await state.clear()
        return
    if is_gift and not is_launch_gift_plan(plan):
        await cb.answer("Этот подарочный тариф недоступен", show_alert=True)
        await state.clear()
        return
    back_to_plans_callback = "gift_back_to_plans" if is_gift else "back_to_plans"
    await state.update_data(
        plan_id=plan.id,
        gift_purchase=is_gift,
        gift_recipient_id=gift_recipient_id,
        promo_code=promo_code,
    )

    if method == "stripe":
        try:
            result = await StripeProvider.create_checkout(
                session,
                user,
                plan,
                gift_recipient_id,
                is_gift=is_gift,
                promo_code=promo_code,
            )
        except Exception:
            await _checkout_failed(
                cb,
                state,
                plan,
                method="stripe",
                user=user,
                promo_code=promo_code,
                is_gift=is_gift,
                back_to_plans_callback=back_to_plans_callback,
                text=(
                    "Не получилось создать счёт для оплаты картой. "
                    "Это на нашей стороне, деньги не списаны.\n\n"
                    "Выберите другой способ оплаты ниже или попробуйте снова через несколько минут. "
                    "Если не выходит — напишите в поддержку, мы оформим вручную."
                ),
            )
            return
        await cb.message.edit_text(
            "Создан счёт. Откройте оплату в браузере — мы откроем доступ автоматически.\n\n"
            f"{PAYMENT_HELP_LINE}",
            reply_markup=checkout_keyboard(
                "💳 Оплатить",
                result.url,
                back_callback_data=f"back_to_methods:{plan.id}",
            )
            if result.url
            else payment_method_keyboard(
                str(plan.id),
                enable_zelle=settings.enable_zelle,
                applied_promo=promo_code,
                allow_promo=not is_gift,
                back_callback_data=back_to_plans_callback,
            ),
        )
        await cb.answer()
        return
    elif method == "lava":
        if settings.enable_lava_live_checkout:
            await state.set_state(BuyFlow.awaiting_lava_email)
            await cb.message.edit_text(
                "Для чека и оформления подписки Lava Top нужен ваш email.\n\n"
                "Отправьте адрес одним сообщением. Мы передадим его только платёжному "
                "провайдеру для этой покупки."
            )
            await cb.answer()
            return
        try:
            result = await LavaProvider.create_checkout(
                session,
                user,
                plan,
                gift_recipient_id,
                is_gift=is_gift,
                promo_code=promo_code,
            )
        except Exception:
            await _checkout_failed(
                cb,
                state,
                plan,
                method="lava",
                user=user,
                promo_code=promo_code,
                is_gift=is_gift,
                back_to_plans_callback=back_to_plans_callback,
                text=(
                    "Lava Top сейчас не создала счёт. Деньги не списаны.\n\n"
                    "Попробуйте ещё раз через несколько минут или выберите другой способ оплаты ниже."
                ),
            )
            return
        await cb.message.edit_text(
            "Оплата через Lava Top: карта РФ и СБП. Сумма фиксированная в ₽.\n\n"
            f"{PAYMENT_HELP_LINE}",
            reply_markup=checkout_keyboard(
                "Оплатить в Lava",
                result.url,
                back_callback_data=f"back_to_methods:{plan.id}",
            )
            if result.url
            else payment_method_keyboard(
                str(plan.id),
                enable_zelle=settings.enable_zelle,
                applied_promo=promo_code,
                allow_promo=not is_gift,
                back_callback_data=back_to_plans_callback,
            ),
        )
        await cb.answer()
        return
    elif method in ("zelle", "usdt_trc20", "usdt_erc20"):
        # GK-120: Zelle is launch-hidden by default; the button is omitted
        # from the keyboard, but reject the callback too in case a stale
        # message lets a user fire the action.
        if method == "zelle" and not settings.enable_zelle:
            await cb.answer("Zelle временно недоступен", show_alert=True)
            return
        try:
            result = await ManualProvider.create_checkout(
                session,
                user,
                plan,
                method,
                gift_recipient_id,
                is_gift=is_gift,
                promo_code=promo_code,
            )
        except Exception:
            await _checkout_failed(
                cb,
                state,
                plan,
                method=method,
                user=user,
                promo_code=promo_code,
                is_gift=is_gift,
                back_to_plans_callback=back_to_plans_callback,
                text=(
                    "Не получилось подготовить реквизиты для оплаты. Деньги не списаны.\n\n"
                    "Выберите другой способ оплаты ниже или попробуйте снова через несколько минут."
                ),
            )
            return
        if method in ("usdt_trc20", "usdt_erc20"):
            deposit_text = (
                result.instructions
                or "Реквизиты USDT отправлены. После оплаты пришлите сюда хэш транзакции."
            )
            await cb.message.edit_text(
                f"{deposit_text}\n\n{USDT_ONE_TIME_NOTE}",
                reply_markup=usdt_awaiting_hash_keyboard(str(plan.id)),
            )
            await state.update_data(
                payment_id=result.payment_id,
                usdt_network="TRC20" if method == "usdt_trc20" else "ERC20",
            )
            await state.set_state(BuyFlow.awaiting_usdt_tx)
            await cb.answer()
            return
        await cb.message.edit_text(result.instructions or "Реквизиты отправлены.")
    else:
        await cb.answer("Неизвестный метод", show_alert=True)
        return
    await state.clear()
    await cb.answer()


# GK-453: the three `BuyFlow.awaiting_*` handlers below each claim every
# message in their state, which is the same trap as the support dialog — a
# member who taps a menu button mid-checkout had the tap read as an email, a
# promo code or a tx hash, and got a validation error instead of the screen they
# asked for. Excluding the menu labels lets the tap fall through to its own
# handler; the FSM state is left alone, so a member who comes back and types a
# real email is still in the flow they started.
@router.message(BuyFlow.awaiting_lava_email, ~F.text.in_(MENU_BUTTON_TEXTS))
async def lava_email_submitted(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    user: User,
):
    # GK-486: the second `create_checkout` call site in this file. Easy to miss
    # — it is a message handler in the middle of an FSM flow, not a button.
    if get_settings().charges_blocked_for(user.tg_id):
        await state.clear()
        await message.answer(
            "Оплата для этого аккаунта отключена — он отмечен как служебный."
        )
        return
    buyer_email = _normalize_buyer_email(message.text)
    if buyer_email is None:
        await message.answer(
            "Не похоже на email. Проверьте адрес и отправьте ещё раз, например name@example.com."
        )
        return

    data = await state.get_data()
    plan = await _load_plan(session, data.get("plan_id"))
    if plan is None:
        await state.clear()
        await message.answer("Сессия оплаты истекла. Начните заново через /subscribe.")
        return

    is_gift = bool(data.get("gift_purchase") or data.get("gift_recipient_id") is not None)
    if is_gift and not is_launch_gift_plan(plan):
        await state.clear()
        await message.answer("Этот подарочный тариф больше недоступен. Начните заново через /gift.")
        return
    try:
        result = await LavaProvider.create_checkout(
            session,
            user,
            plan,
            data.get("gift_recipient_id"),
            is_gift=is_gift,
            promo_code=None if is_gift else data.get("promo_code"),
            buyer_email=buyer_email,
        )
    except LavaCheckoutUnavailable:
        await state.set_state(BuyFlow.choosing_method)
        await message.answer(
            "Lava Top не может оформить этот вариант покупки без изменения суммы или условий. "
            "Выберите Stripe или USDT — скидка и подарок там сохранятся.",
            reply_markup=payment_method_keyboard(
                str(plan.id),
                enable_zelle=settings.enable_zelle,
                applied_promo=None if is_gift else data.get("promo_code"),
                allow_promo=not is_gift,
                back_callback_data="gift_back_to_plans" if is_gift else "back_to_plans",
            ),
        )
        return
    except LavaAPIError:
        logger.exception("lava create checkout failed user_id=%s plan_id=%s", user.id, plan.id)
        await state.set_state(BuyFlow.choosing_method)
        await message.answer(
            "Lava Top сейчас не создала счёт. Попробуйте ещё раз позже или выберите другой способ оплаты.",
            reply_markup=payment_method_keyboard(
                str(plan.id),
                enable_zelle=settings.enable_zelle,
                applied_promo=None if is_gift else data.get("promo_code"),
                allow_promo=not is_gift,
                back_callback_data="gift_back_to_plans" if is_gift else "back_to_plans",
            ),
        )
        return
    except Exception:
        # GK-421: this branch handled the two Lava-specific failures and let
        # everything else (network, DB, a provider SDK surprise) escape into
        # the same silence the callback sites had.
        logger.exception(
            "lava create checkout failed unexpectedly user_id=%s plan_id=%s", user.id, plan.id
        )
        await state.set_state(BuyFlow.choosing_method)
        await message.answer(
            "Не получилось создать счёт. Деньги не списаны.\n\n"
            "Попробуйте ещё раз через несколько минут или выберите другой способ оплаты.",
            reply_markup=payment_method_keyboard(
                str(plan.id),
                enable_zelle=settings.enable_zelle,
                applied_promo=None if is_gift else data.get("promo_code"),
                allow_promo=not is_gift,
                back_callback_data="gift_back_to_plans" if is_gift else "back_to_plans",
            ),
        )
        try:
            await send_ops_alert(
                # GK-451: plain text, see the sibling alert above.
                "Не удалось создать счёт: lava (email branch)\n"
                f"user_id={user.id} plan_id={plan.id}",
                key="checkout_failed:lava_email",
                rate_limit_seconds=300,
                severity="error",
            )
        except Exception:  # noqa: BLE001 — alerting is not allowed to break the reply
            logger.warning("could not send ops alert for lava email checkout failure", exc_info=True)
        return

    await state.set_state(BuyFlow.choosing_method)
    await message.answer(
        "Счёт Lava Top создан. Оплата откроется в защищённом окне провайдера; "
        "после успешного платежа доступ включится автоматически.\n\n"
        f"{PAYMENT_HELP_LINE}",
        reply_markup=checkout_keyboard(
            "Оплатить в Lava",
            result.url,
            back_callback_data=f"back_to_methods:{plan.id}",
        ),
    )


@router.callback_query(F.data.startswith("promo:"))
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    plan_id = _callback_id(cb.data, "promo:")
    data = await state.get_data()
    if not plan_id and not data.get("plan_id"):
        await cb.answer("Сессия истекла", show_alert=True)
        return
    if plan_id is not None and data.get("plan_id") != plan_id:
        await state.update_data(plan_id=plan_id, promo_code=None)
    await state.set_state(BuyFlow.awaiting_promo)
    await cb.message.answer(
        "Введите промокод одним сообщением.\n"
        "Можно продолжить без него — просто выберите способ оплаты в предыдущем сообщении."
    )
    await cb.answer()


@router.message(BuyFlow.awaiting_promo, ~F.text.in_(MENU_BUTTON_TEXTS))
async def promo_submitted(message: Message, session: AsyncSession, state: FSMContext, user: User):
    data = await state.get_data()
    plan_id = data.get("plan_id")
    plan = await _load_plan(session, plan_id)
    if not plan:
        await state.clear()
        await message.answer("Сессия истекла. Начните /subscribe заново.")
        return

    code = normalize_code(message.text or "")
    validation = None
    if code:
        validation = await validate_promo_code(
            session,
            code,
            user,
            plan,
            base_amount=plan.price_usd,
            currency="USD",
            is_gift=data.get("gift_recipient_id") is not None,
        )

    await state.set_state(BuyFlow.choosing_method)
    if validation is not None and validation.valid:
        await state.update_data(promo_code=code)
        await message.answer(
            f"🎟 Промокод <b>{html.escape(code)}</b> применён. Выберите способ оплаты:",
            reply_markup=payment_method_keyboard(
                str(plan_id),
                enable_zelle=settings.enable_zelle,
                applied_promo=code,
                back_callback_data="back_to_plans",
            ),
        )
        return

    reason = _promo_error_message(validation.status if validation else "not_found")
    await message.answer(
        f"{reason} Выберите способ оплаты или нажмите 🎟 ещё раз, чтобы ввести другой код:",
        reply_markup=payment_method_keyboard(
            str(plan_id),
            enable_zelle=settings.enable_zelle,
            applied_promo=data.get("promo_code"),
            back_callback_data="back_to_plans",
        ),
    )


@router.message(BuyFlow.awaiting_usdt_tx, ~F.text.in_(MENU_BUTTON_TEXTS))
async def usdt_tx_submitted(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    user: User,
    bot: Bot,
):
    data = await state.get_data()
    payment_id = data.get("payment_id")
    network = data.get("usdt_network")
    tx_hash = extract_tx_hash(message.text or "")
    if not payment_id or not network:
        await state.clear()
        await message.answer("Сессия оплаты истекла. Начните заново через /subscribe.")
        return
    if tx_hash is None:
        await message.answer("Пришлите только хэш транзакции USDT (64 шестнадцатеричных символа).")
        return

    payment = (
        await session.execute(
            select(Payment)
            .where(
                Payment.id == int(payment_id),
                Payment.user_id == user.id,
                Payment.provider == "usdt",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if payment is None or payment.status != "awaiting_review":
        await state.clear()
        await message.answer("Этот платёж больше не ожидает хэш транзакции.")
        return

    result = await verify_and_apply_usdt_payment(
        session,
        payment,
        tx_hash,
        bot=bot,
        network=str(network),
    )
    await state.clear()

    if result.is_valid:
        plan = None
        if payment.plan_id is not None:
            plan = (
                await session.execute(select(Plan).where(Plan.id == payment.plan_id))
            ).scalar_one_or_none()
        subscription = None
        if result.subscription_id is not None:
            subscription = (
                await session.execute(
                    select(Subscription).where(Subscription.id == result.subscription_id)
                )
            ).scalar_one_or_none()
        try:
            if payment.is_gift and payment.gift_recipient_id is None:
                gift = await ensure_paid_gift_activation(session, payment)
                if gift is not None:
                    await message.answer(
                        build_paid_gift_activation_message(payment, plan, gift)
                    )
                    return
            await message.answer(
                build_payment_succeeded_message(payment, plan, subscription)
            )
        except Exception:
            logger.exception(
                "billing notification usdt_bot_success failed; continuing payment flow payment_id=%s",
                payment.id,
            )
        return

    if result.needs_manual_review:
        await message.answer(
            "Хэш получен, но автопроверка пока не смогла подтвердить платёж — "
            "его проверит куратор вручную. Доступ откроется после подтверждения, обычно в течение часа."
        )
        return

    await message.answer(
        "Эту транзакцию USDT не удалось принять автоматически: "
        f"{result.message} Проверьте хэш или напишите в /support."
    )


@router.callback_query(F.data == "cancel")
async def cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.answer()


@router.callback_query(F.data == "buy_start")
async def buy_start_cb(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
    await _show_plan_selection_callback(cb, session, state)


@router.callback_query(F.data == "back_to_plans")
async def back_to_plans_cb(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    await _show_plan_selection_callback(cb, session, state)


@router.callback_query(F.data.startswith("back_to_methods:"))
async def back_to_methods_cb(cb: CallbackQuery, session: AsyncSession, state: FSMContext, user: User):
    plan_id = _callback_id(cb.data, "back_to_methods:")
    plan = await _load_plan(session, plan_id)
    if not plan:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    data = await state.get_data()
    await _show_payment_methods(
        cb,
        session,
        state,
        user,
        plan,
        gift_recipient_id=data.get("gift_recipient_id"),
        is_gift=bool(data.get("gift_purchase")),
        promo_code=data.get("promo_code") if data.get("plan_id") == plan.id else None,
    )
    await cb.answer()


@router.callback_query(F.data == "offer_doc")
async def offer_document_cb(cb: CallbackQuery):
    # Method-agnostic: the same offer PDF is delivered regardless of which
    # payment method the user is looking at. Keep the current screen intact —
    # send the document as a new message rather than editing the payment text.
    if not OFFER_DOCUMENT_PATH.exists():
        logger.error("offer document missing at %s", OFFER_DOCUMENT_PATH)
        await cb.answer("Документ оферты временно недоступен. Напишите в /support.", show_alert=True)
        return
    await cb.message.answer_document(
        FSInputFile(str(OFFER_DOCUMENT_PATH), filename=OFFER_DOCUMENT_FILENAME),
        caption=OFFER_DOCUMENT_CAPTION,
    )
    await cb.answer()


@router.callback_query(F.data == "sub_back")
async def sub_back_cb(cb: CallbackQuery, session: AsyncSession, state: FSMContext, user: User):
    await state.clear()
    sub, plan = await _subscription_context(session, user)
    if sub is None:
        await cb.message.edit_text("Ок, вернулись назад. Главное меню можно открыть через /start.")
        await cb.answer()
        return
    await cb.message.edit_text(
        _subscription_manage_text(sub, plan),
        reply_markup=subscription_management_keyboard(
            can_cancel_autorenew=_can_cancel_autorenew(sub),
            cancel_requested=has_cancellation_on_record(sub),
        ),
    )
    await cb.answer()


@router.callback_query(F.data == "subscription_manage")
async def subscription_manage_cb(cb: CallbackQuery, session: AsyncSession, state: FSMContext, user: User):
    await _render_subscription_management(cb, session, user, state)


@router.callback_query(F.data == "sub_cancel_start")
async def subscription_cancel_start(cb: CallbackQuery, session: AsyncSession, state: FSMContext, user: User):
    await state.clear()
    sub, plan = await _subscription_context(session, user)
    if sub is None:
        await cb.answer("Активная подписка не найдена", show_alert=True)
        return
    access_until = _format_date(subscription_access_ends_at(sub))
    plan_name = html.escape(getattr(plan, "name", None) or "текущий тариф")
    await cb.message.edit_text(
        "<b>Отменить автопродление?</b>\n\n"
        f"Тариф: <b>{plan_name}</b>\n"
        f"Доступ останется активным до <b>{access_until}</b>.\n\n"
        "Мы отключим автопродление у платёжного провайдера. Если сделать это автоматически "
        "не получится, запрос примет администратор и доведёт отмену вручную — в любом случае "
        "мы напишем, чем всё закончилось.\n\n"
        "Возврат средств через бот не открывается; если нужен разбор оплаты, напишите в поддержку.",
        reply_markup=subscription_cancel_confirm_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data == "sub_cancel_confirm")
async def subscription_cancel_confirm(cb: CallbackQuery, session: AsyncSession, state: FSMContext, user: User):
    await state.clear()
    sub, plan = await _subscription_context(session, user)
    if sub is None:
        await cb.answer("Активная подписка не найдена", show_alert=True)
        return
    access_until = _format_date(subscription_access_ends_at(sub))
    outcome = await request_autorenew_cancellation(session, sub, user=user)

    if outcome.provider_confirmed:
        # Only said when the provider actually answered "yes".
        text = (
            "<b>Автопродление отключено.</b>\n\n"
            f"Списаний больше не будет. Доступ сохраняется до <b>{access_until}</b>."
        )
    else:
        # Honest fallback: we could not stop the charge ourselves. Saying
        # anything stronger here is what let two curators keep being charged.
        text = (
            "<b>Запрос на отмену автопродления принят.</b>\n\n"
            f"Доступ сохраняется до <b>{access_until}</b>.\n\n"
            "⚠️ Отключение у платёжного провайдера мы пока <b>не подтвердили</b> — "
            "это сделает администратор вручную и вернётся к вам с подтверждением. "
            "Если до даты списания подтверждения не будет, напишите в поддержку."
        )
    await cb.message.edit_text(
        text,
        reply_markup=subscription_management_keyboard(
            can_cancel_autorenew=_can_cancel_autorenew(sub),
            cancel_requested=True,
        ),
    )
    await cb.answer()


@router.callback_query(F.data == "sub_cancel_already")
async def subscription_cancel_already(cb: CallbackQuery, session: AsyncSession, user: User):
    sub = await get_active_subscription(session, user.id)
    if effective_cancel_state(sub) == CANCEL_PROVIDER_CONFIRMED:
        message = "Автопродление отключено. Списаний больше не будет, доступ остаётся до оплаченной даты."
    else:
        message = (
            "Запрос на отмену уже принят, отключение у провайдера ещё подтверждается администратором. "
            "Доступ остаётся до оплаченной даты."
        )
    await cb.answer(message, show_alert=True)
