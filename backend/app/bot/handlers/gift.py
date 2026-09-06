import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.subscription import BuyFlow
from app.bot.keyboards import (
    main_menu,
    payment_method_keyboard,
    plan_price_label,
    plans_keyboard,
)
from app.db.models import Plan
from app.services.gifts import is_launch_gift_plan

router = Router(name="gift")

# GK-419: Grant's 24.07 doc (Tab 2) rewrote the gift blurb. «на 30 дней» moves out
# of the first paragraph, and three paragraphs are added: link validity + when the
# term actually starts, no auto-renewal, and the support pointer. Verbatim.
# Single constant because the message-entry and callback-entry paths must never
# drift apart (they did have two hand-kept copies before).
GIFT_INTRO = (
    "<b>🎁 Подарить подписку</b>\n\n"
    "Иногда лучший подарок это дверь к самому себе. "
    "Выберите срок и способ оплаты. Получателя заранее указывать не нужно, "
    "после оплаты вы получите одноразовую ссылку активации.\n\n"
    "Ссылка действует 30 дней. Подписка начинается с того дня, когда получатель "
    "её активирует, и длится выбранный вами срок.\n\n"
    "Подарок не продлевается автоматически. Когда срок закончится, получатель "
    "сможет продолжить сам на любом тарифе.\n\n"
    "Вопросы → /support"
)


@router.message(Command("gift"))
@router.message(F.text == "🎁 Подарить")
async def gift_entry(message: Message, session: AsyncSession, state: FSMContext):
    await _show_gift_plans_message(message, session, state)


@router.callback_query(F.data == "gift_start")
async def gift_start_cb(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    await _show_gift_plans_callback(cb, session, state)


@router.callback_query(F.data.startswith("gift_plan:"))
async def gift_plan_chosen(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    plan_id = int(cb.data.split(":")[1])
    plan = (await session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if not plan or not is_launch_gift_plan(plan):
        await cb.answer("Этот подарочный тариф недоступен", show_alert=True)
        return

    await state.update_data(
        plan_id=plan_id,
        gift_purchase=True,
        gift_recipient_id=None,
        promo_code=None,
    )
    await state.set_state(BuyFlow.choosing_method)
    safe_plan_name = html.escape(plan.name)
    await cb.message.edit_text(
        f"<b>🎁 Подарочная подписка: {safe_plan_name}</b>\n"
        f"{plan_price_label(plan, include_lava_hint=True)}\n\n"
        "После оплаты бот пришлёт вам одноразовую ссылку активации. "
        "Отправьте её получателю: он нажмёт ссылку, откроет бота и получит доступ.\n"
        "Ссылка действует 30 дней. Скидки и партнёрские начисления на подарки не применяются.\n\n"
        "Как оплатите?",
        reply_markup=payment_method_keyboard(
            str(plan_id),
            allow_promo=False,
            back_callback_data="gift_back_to_plans",
        ),
    )
    await cb.answer()


@router.callback_query(F.data == "gift_back_to_plans")
async def gift_back_to_plans(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    await _show_gift_plans_callback(cb, session, state)


@router.callback_query(F.data == "gift_back")
async def gift_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Подарок отменён. Главное меню можно открыть через /start.")
    await cb.answer()


async def _active_plans(session: AsyncSession) -> list[Plan]:
    plans = (
        await session.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order)
        )
    ).scalars().all()
    return [plan for plan in plans if is_launch_gift_plan(plan)]


async def _show_gift_plans_message(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    plans = await _active_plans(session)
    if not plans:
        await message.answer("Тарифы временно недоступны. Загляните позже.", reply_markup=main_menu())
        return
    await state.update_data(
        plan_id=None,
        gift_purchase=True,
        gift_recipient_id=None,
        promo_code=None,
    )
    await state.set_state(BuyFlow.choosing_plan)
    await message.answer(
        GIFT_INTRO,
        reply_markup=plans_keyboard(list(plans), for_gift=True, back_callback_data="gift_back"),
    )


async def _show_gift_plans_callback(
    cb: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    plans = await _active_plans(session)
    if not plans:
        await cb.message.edit_text("Тарифы временно недоступны. Загляните позже.")
        await cb.answer()
        return
    await state.update_data(
        plan_id=None,
        gift_purchase=True,
        gift_recipient_id=None,
        promo_code=None,
    )
    await state.set_state(BuyFlow.choosing_plan)
    await cb.message.edit_text(
        GIFT_INTRO,
        reply_markup=plans_keyboard(list(plans), for_gift=True, back_callback_data="gift_back"),
    )
    await cb.answer()
