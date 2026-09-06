import html

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import main_menu
from app.db.models import Payment, Subscription, User
from app.services.gifts import GIFT_TOKEN_PREFIX, redeem_gift_token
from app.services.referral import link_referral_code

router = Router(name="start")

# GK-452: welcome copy is Grant's rewritten wording from his 19.08 doc (Tab 3),
# replacing the GK-419 version. Each of the seven ✅ bullets is now a headline
# plus its own explanation, a new «Внутри уже идёт жизнь» paragraph sits before
# the closing one, and the referral line shortens to «🔹 Друзьям скидка 20% на
# первый месяц».
#
# Bold: Grant's «жирным идут только названия пунктов» names the new spans — the
# seven bullet headlines. It is not an instruction to strip the two <b> the
# screen already had (the title and «💎 Подписка»), which his doc simply does not
# restate; they carry over from GK-419 unchanged. Nine <b> in total.
#
# The 🍄 fly agaric is replaced by ⭐ per Grant's «в крайнем случае ⭐» — the
# MycoTotems brand logo he prefers is a custom emoji, which this bot cannot send
# unless its owner account has Telegram Premium (or the bot holds a Fragment
# username). See GK-420; do not put 🍄 back.
WELCOME = (
    "<b>Закрытое Сообщество Павла Дмитриева | Membership Club</b> ⭐\n\n"
    "Живое пространство работы с собой и инструменты, которые реально меняют "
    "жизнь. Двадцать лет практики и сотни тысяч людей, прошедших через эту "
    "работу.\n\n"
    "Внутри вас ждёт 👇\n\n"
    "✅ <b>Безлимитные проработки в чате практики.</b> В любой момент вы можете "
    "попросить о помощи, и опытный гипнотерапевт возьмёт вас в работу. "
    "Не через месяц по записи и без дополнительных оплат. Одной проработки "
    "бывает достаточно, чтобы жизнь развернулась\n"
    "✅ <b>Архив Гипно-Коучинга, более 3000 уроков.</b> Методология и построение "
    "собственной практики. Плюс библиотека материалов: документы, регламенты, "
    "шаблоны договоров, всё для запуска практики и выхода на доход\n"
    "✅ <b>Еженедельные эфиры и разборы.</b> Что мешает, чего не хватает, что "
    "делать дальше. Вы растёте и через разборы других\n"
    "✅ <b>Эфиры с шаманами и экспертами.</b> Наши шаманы из Мексики, Эквадора и "
    "Колумбии. И приглашённые эксперты: восточная философия, ведические знания, "
    "наука, здоровье, деньги\n"
    "✅ <b>Идеи мировых мастеров.</b> Хормози, Роббинс, Кеннеди, Овенс, Голден. "
    "Их программы стоят десятки тысяч долларов, и мы их проходим. То, что "
    "применили и что дало результат в реальном бизнесе, становится частью "
    "работы Сообщества\n"
    "✅ <b>Главы Кодекса Микомистицизма.</b> Философская конституция движения\n"
    "✅ <b>Ранний доступ к Sacred Mushroom University.</b> Интеграция опыта, "
    "помощь другим, путь проводника\n\n"
    "Внутри уже идёт жизнь. Пока вы читаете это, кто-то там делает свой "
    "следующий шаг.\n\n"
    "Главное, что даёт Сообщество, вы перестаёте идти в одиночку и "
    "начинаете расти рядом с теми, кто идёт тем же путём.\n\n"
    "🔹 Доступ по подписке\n"
    "🔹 Друзьям скидка 20% на первый месяц\n"
    "🔹 Можно подарить подписку близкому\n\n"
    "Нажмите <b>💎 Подписка</b>, чтобы войти 👇"
)


@router.message(CommandStart(deep_link=True))
async def start_with_ref(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    bot: Bot | None = None,
):
    # /start is the universal escape hatch: drop any in-flight FSM flow so a
    # user stuck mid-dialog (GK-350) always gets back to the menu.
    await state.clear()
    # Limit how much of `args` we ever touch. Long deep-links should not
    # reach the DB at all.
    arg = (command.args or "").strip()[:128]
    if arg.startswith("ref_"):
        # Whitelisting referral codes to alphanumeric (matches generate_referral_code)
        # also protects against attempts to pass weird/long input.
        code = arg[4:]
        result = None
        if (1 <= len(code) <= 16) and code.isalnum():
            result = await link_referral_code(session, code, user)
        if result is not None and result.status in {"linked", "already_linked"} and result.referrer:
            # Escape username so a referrer can't HTML-inject into the welcome msg
            # of their own referees.
            safe_name = html.escape(result.referrer.username or "друг")
            await message.answer(
                f"🎉 Вас пригласил <b>@{safe_name}</b>. "
                "Скидка 20% применится автоматически на первый месячный тариф."
            )
    elif arg.startswith(GIFT_TOKEN_PREFIX):
        result = await redeem_gift_token(session, arg, user, bot=bot)
        if result.ok:
            invite_link = result.invite_link or getattr(result.subscription, "invite_link", None)
            lines = [
                "🎁 <b>Подарочная подписка активирована.</b>",
                "Доступ включает закрытый канал сообщества и чат практики.",
            ]
            if invite_link:
                lines.extend(["", "Ваши одноразовые ссылки:", invite_link])
            else:
                lines.append(
                    "Ссылки доступа не удалось создать автоматически. Напишите /support, куратор выпустит их вручную."
                )
            await message.answer("\n".join(lines))
        elif result.status == "already_redeemed":
            await message.answer("Эта подарочная ссылка уже была активирована. Если это ошибка, напишите /support.")
        elif result.status == "expired":
            await message.answer("Срок действия подарочной ссылки истёк. Попросите отправителя написать в /support.")
        elif result.status == "payment_pending":
            await message.answer("Подарок ещё ожидает подтверждения оплаты. Попробуйте позже или напишите /support.")
        else:
            await message.answer("Подарочная ссылка недействительна. Проверьте ссылку или напишите /support.")
    elif arg.startswith("paid_"):
        await message.answer("✅ Оплата получена! Проверяю статус…")
        payment_id_str = arg[5:]
        invite_link = None
        if payment_id_str.isdigit() and len(payment_id_str) <= 18:
            payment_id = int(payment_id_str)
            # Give the webhook handler up to 5 seconds to commit
            import asyncio
            for _ in range(5):
                await asyncio.sleep(1)
                sub = (
                    await session.execute(
                        select(Subscription)
                        .join(Payment, Payment.user_id == Subscription.user_id)
                        .where(
                            Payment.id == payment_id,
                            Payment.user_id == user.id,  # require ownership
                            Payment.status == "succeeded",
                            Subscription.status == "active",
                            Subscription.invite_link.isnot(None),
                        )
                    )
                ).scalar_one_or_none()
                if sub:
                    invite_link = sub.invite_link
                    break
        if invite_link:
            await message.answer(
                "🎉 Подписка активирована!\n\n"
                "Доступ включает канал сообщества и чат практик с гипнотерапевтами.\n\n"
                f"Ваши одноразовые ссылки:\n{invite_link}\n\n"
                "⚠️ Ссылки одноразовые — не передавайте их другим."
            )
        else:
            await message.answer(
                "⏳ Платёж обрабатывается. Ссылка придёт отдельным сообщением в течение минуты.\n"
                "Если не получите — напишите /support."
            )
    await message.answer(WELCOME, reply_markup=main_menu())


@router.message(CommandStart())
async def start_basic(message: Message, user: User, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=main_menu())


@router.message(F.text == "ℹ️ О сообществе")
async def about(message: Message):
    # GK-452: "ℹ️ О сообществе" copy is Grant's rewritten wording (19.08 doc,
    # Tab 3), replacing the GK-419 version. Three changes, all his: the title
    # gains «| Membership Club ⭐» and keeps its bold; a «К чему это приводит»
    # paragraph is added; «Никого никуда не тянут.» is dropped, and the closing
    # «Главное, что здесь есть» becomes its own paragraph, with a colon and the
    # «путь… проходится быстрее» clause. Verbatim, including the capitalised
    # «Сообщество» and the clipped «Или знает, что она впереди.»
    await message.answer(
        "<b>Закрытое Сообщество Павла Дмитриева | Membership Club ⭐</b>\n\n"
        "Для тех, кто проходит кризис или тёмную ночь души. "
        "Или знает, что она впереди.\n\n"
        "Кризис это не поломка. Это точка роста, а рост всегда идёт через "
        "сопротивление. Здесь вы получаете инструменты, которые помогают пройти "
        "этот участок пути и собрать себя заново. Гипнотерапия, работа с "
        "подсознанием, интеграция психоделического опыта.\n\n"
        "К чему это приводит. Уходит то, что держало годами. Возвращается "
        "ясность и силы. Появляется понимание, куда двигаться дальше. Для тех, "
        "кто идёт глубже, это становится профессией и делом жизни.\n\n"
        "Каждый идёт на своём уровне. Кто-то просто прорабатывает своё и меняет "
        "качество жизни. Кто-то идёт дальше, к обучению и пути проводника.\n\n"
        "Растения-учителя это серьёзно. Мы учим понимать, а не пробовать. "
        "Этот путь проходят с опытными проводниками и только там, где это "
        "разрешено законом.\n\n"
        "Двадцать лет практики и сотни тысяч людей, прошедших через эту "
        "работу.\n\n"
        "Главное, что здесь есть: вы идёте не в одиночку, и путь, который в "
        "одиночку занимает годы, здесь проходится быстрее.\n\n"
        "Вопросы → /support"
    )
