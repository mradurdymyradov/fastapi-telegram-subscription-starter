"""AI support assistant adapter.

Three providers: mock | openai | anthropic — selected via SUPPORT_AI_PROVIDER env var.
Mock answers a curated FAQ in Russian without burning any API credit; the OpenAI / Anthropic
adapters use the same system prompt and history shape so swapping is a one-line change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


SYSTEM_PROMPT = """Ты — ассистент поддержки закрытого сообщества Павла Дмитриева — пространства для тех, кто проходит кризис или тёмную ночь души (интеграция психоделического опыта, выход из кризиса, работа с подсознанием; архив 3000+ уроков Гипно-Коучинга).
Отвечай по-русски, кратко и по делу. Помогай с вопросами про:
- подписку (тарифы, продление, отмена)
- оплату (Stripe, Lava Top, Zelle, USDT)
- доступ к закрытому каналу
- реферальную программу (скидка 20% другу на первый месячный тариф)
- подарочные подписки

Если вопрос за пределами твоей зоны — предложи связаться с куратором сообщества через команду /support.
Никогда не выдумывай цены, даты или личную информацию.
"""


@dataclass
class ChatMessage:
    role: str  # user | assistant
    content: str


MOCK_FAQ: list[tuple[tuple[str, ...], str]] = [
    (
        ("как подписат", "как оформит", "хочу подписат", "хочу оформит", "как купит", "купить подписк", "оформить подписк", "подписаться", "subscribe"),
        "Чтобы оформить подписку:\n1. Нажмите кнопку <b>💎 Подписка</b> в меню (или /subscribe)\n2. Выберите тариф\n3. Выберите способ оплаты\n4. После оплаты бот пришлёт ссылку в закрытый канал.",
    ),
    (
        ("тариф", "цена", "стоимость", "сколько стоит", "подписк", "сколько", "план"),
        "У нас 3 тарифа:\n• 1 месяц — $19 / 1500 ₽\n• 6 месяцев — $79 / 7000 ₽\n• Год — $129 / 10000 ₽\n\nОткройте /subscribe чтобы выбрать.",
    ),
    (
        ("оплат", "карт", "stripe", "lava", "как платить", "способ оплаты"),
        "Принимаем: Stripe (карты Visa/MC), Lava Top, Zelle и USDT (TRC20/ERC20). Выберите в /subscribe.",
    ),
    (
        ("zelle", "usdt"),
        "Для Zelle/USDT: выберите способ в /subscribe, бот покажет реквизиты, после оплаты пришлите скриншот — куратор подтвердит в течение часа.",
    ),
    (
        ("реферал", "пригласить", "друг"),
        "Реф-ссылка находится в личном кабинете: /cabinet. Друг получит скидку 20% на первый месячный тариф, а реферальное начисление фиксируется после его первой оплаты.",
    ),
    (
        ("подарок", "подарить"),
        "Подарить подписку: /cabinet → «Подарить подписку» → введите @username получателя → выберите тариф → оплата. Получатель получит уведомление и инвайт сразу после оплаты.",
    ),
    (
        ("канал", "доступ", "не пуска"),
        "После успешной оплаты бот пришлёт одноразовую инвайт-ссылку. Если ссылка устарела или не работает — напишите /support, мы выпустим новую.",
    ),
    (
        ("отмен", "вернуть", "рефанд"),
        "Возврат возможен в течение 7 дней после первой оплаты, если вы не пользовались каналом. Напишите /support — куратор оформит.",
    ),
    (
        ("продл", "истек", "закончится"),
        "За 3 дня до окончания подписки бот пришлёт напоминание. Продлить можно в любой момент через /subscribe — дни прибавятся к текущему сроку.",
    ),
    (
        ("куратор", "связаться", "человек"),
        "Куратор сообщества — @membership_curator. Также можете написать /support, мы получим уведомление.",
    ),
    (
        ("привет", "здравствуй", "хай"),
        "Здравствуйте! Я ассистент membership_saas. Чем помочь — подписка, оплата, реферал или подарок?",
    ),
]


def _mock_reply(question: str) -> str:
    q = question.lower()
    for keywords, answer in MOCK_FAQ:
        if any(k in q for k in keywords):
            return answer
    return (
        "Не уверен, как ответить — могу помочь с подпиской, оплатой, доступом в канал, "
        "рефералами и подарками. Сформулируйте, что именно интересует, или напишите /support — "
        "куратор ответит лично."
    )


async def _openai_reply(history: list[ChatMessage]) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}]
        + [{"role": m.role, "content": m.content} for m in history],
        temperature=0.4,
        max_tokens=400,
    )
    return resp.choices[0].message.content or ""


async def _anthropic_reply(history: list[ChatMessage]) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": m.role, "content": m.content} for m in history],
    )
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


async def get_support_reply(history: list[ChatMessage]) -> str:
    """Main entry. history = list of past messages (oldest first), last one is user's latest."""
    if not history:
        return "Здравствуйте! Чем могу помочь?"
    # GK-120: production launch ships with ENABLE_AI_SUPPORT=false so the
    # bot never reaches OpenAI/Anthropic. The mock FAQ keeps the same UX
    # without burning API credit; SUPPORT_AI_PROVIDER is honored only when
    # the flag is explicitly opted in.
    if not settings.enable_ai_support:
        return _mock_reply(history[-1].content)
    provider = settings.support_ai_provider.lower()
    try:
        if provider == "openai" and settings.openai_api_key:
            return await _openai_reply(history)
        if provider == "anthropic" and settings.anthropic_api_key:
            return await _anthropic_reply(history)
    except Exception as e:
        logger.exception("AI provider failed, falling back to mock: %s", e)
    return _mock_reply(history[-1].content)
