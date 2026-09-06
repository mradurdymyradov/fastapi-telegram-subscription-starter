"""Manual payment flow: Zelle / USDT (TRC20/ERC20).

UX:
1. User picks Zelle or USDT in /subscribe.
2. Bot creates a Payment row with status='awaiting_review' and shows recipient details.
3. Zelle stays manual; USDT asks for a tx hash and attempts auto-verification.
4. Ambiguous USDT or Zelle payments stay in the admin review queue.
5. Admin reviews in the panel → approve creates subscription + sends invite, or reject notifies user.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Payment, Plan, User
from app.payments.base import CheckoutResult
from app.services.promo import discount_for_checkout

settings = get_settings()


def _instructions(method: str, plan: Plan, payment_id: int, amount_usd: object) -> str:
    if method == "zelle":
        return (
            f"<b>Zelle оплата — {plan.name} (${amount_usd})</b>\n\n"
            f"Получатель: <code>{settings.zelle_recipient}</code>\n"
            f"Сумма: <b>${amount_usd}</b>\n"
            f"Заметка к платежу: <code>membership_saas-{payment_id}</code>\n\n"
            "После оплаты пришлите сюда скриншот подтверждения. "
            "Куратор проверит платёж в течение часа и откроет доступ."
        )
    if method == "usdt_trc20":
        return (
            f"<b>USDT TRC20 — {plan.name} (${amount_usd})</b>\n\n"
            f"Адрес: <code>{settings.usdt_trc20_address}</code>\n"
            f"Сеть: <b>TRC20 (Tron)</b>\n"
            f"Сумма: <b>{amount_usd} USDT</b>\n\n"
            "Пришлите сюда хэш транзакции или скриншот. Куратор проверит и откроет доступ."
        )
    return (
        f"<b>USDT ERC20 — {plan.name} (${amount_usd})</b>\n\n"
        f"Адрес: <code>{settings.usdt_erc20_address}</code>\n"
        f"Сеть: <b>ERC20 (Ethereum)</b>\n"
        f"Сумма: <b>{amount_usd} USDT</b>\n\n"
        "Пришлите сюда хэш транзакции или скриншот."
    )


class ManualProvider:
    name = "manual"

    @staticmethod
    async def create_checkout(
        session: AsyncSession,
        user: User,
        plan: Plan,
        method: str,  # zelle | usdt_trc20 | usdt_erc20
        gift_recipient_id: int | None = None,
        *,
        is_gift: bool = False,
        promo_code: str | None = None,
    ) -> CheckoutResult:
        is_gift = is_gift or gift_recipient_id is not None
        provider_code = "zelle" if method == "zelle" else "usdt"
        tx_network = None
        if method == "usdt_trc20":
            tx_network = "TRC20"
        elif method == "usdt_erc20":
            tx_network = "ERC20"
        discount = await discount_for_checkout(
            session,
            user,
            plan,
            plan.price_usd,
            "USD",
            promo_code=promo_code,
            is_gift=is_gift,
        )
        note = f"method={method}"
        if discount.applied:
            note += f"; {discount.kind}_discount={discount.code}"
        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider=provider_code,
            amount=discount.amount,
            currency="USD",
            status="awaiting_review",
            is_gift=is_gift,
            gift_recipient_id=gift_recipient_id,
            note=note,
            tx_network=tx_network,
        )
        session.add(payment)
        await session.flush()
        discount.link_payment(payment)
        return CheckoutResult(
            payment_id=payment.id,
            url=None,
            instructions=_gift_instructions(
                _instructions(method, plan, payment.id, discount.amount)
            )
            if is_gift
            else _instructions(method, plan, payment.id, discount.amount),
        )


def _gift_instructions(instructions: str) -> str:
    return (
        f"{instructions}\n\n"
        "После подтверждения оплаты бот пришлёт вам одноразовую ссылку активации подарка. "
        "Получатель нажмёт её сам; заранее указывать @username не нужно."
    )
