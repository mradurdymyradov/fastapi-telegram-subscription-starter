from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Gift, Payment, Plan, Subscription, User, utcnow
from app.services.channel_access import create_invite_links
from app.services.subscription import create_or_extend_subscription
from app.services.webhooks import dispatch

settings = get_settings()

GIFT_TOKEN_PREFIX = "gift_"
_LAUNCH_GIFT_TERMS = {
    "1m": (28, 31),
    "6m": (175, 186),
    "12m": (360, 370),
}
_GIFT_TERM_ALIASES = {
    "month": "1m",
    "monthly": "1m",
    "half_year": "6m",
    "semiannual": "6m",
    "year": "12m",
    "annual": "12m",
}


@dataclass(frozen=True)
class GiftRedemptionResult:
    status: str
    gift: Gift | None = None
    subscription: Subscription | None = None
    invite_link: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "redeemed"


def launch_gift_term(plan: Plan) -> str | None:
    """Return the canonical 1m/6m/12m term for an active launch gift plan."""
    if not bool(getattr(plan, "is_active", False)):
        return None
    code = str(getattr(plan, "code", "") or "").strip().lower()
    term = _GIFT_TERM_ALIASES.get(code, code)
    bounds = _LAUNCH_GIFT_TERMS.get(term)
    if bounds is None:
        return None
    try:
        duration_days = int(getattr(plan, "duration_days", 0))
    except (TypeError, ValueError):
        return None
    return term if bounds[0] <= duration_days <= bounds[1] else None


def is_launch_gift_plan(plan: Plan) -> bool:
    return launch_gift_term(plan) is not None


async def ensure_paid_gift_activation(
    session: AsyncSession,
    payment: Payment,
) -> Gift | None:
    """Create or load the one-time activation record for a paid unclaimed gift."""
    if not _is_unclaimed_gift_payment(payment):
        return None

    existing = (
        await session.execute(select(Gift).where(Gift.payment_id == payment.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    gift = Gift(
        sender_id=payment.user_id,
        receiver_id=None,
        plan_id=payment.plan_id,
        payment_id=payment.id,
    )
    session.add(gift)
    await session.flush()
    return gift


def gift_activation_token(gift: Gift) -> str:
    return f"{GIFT_TOKEN_PREFIX}{gift.id}_{_signature(gift)}"


def gift_activation_url(gift: Gift) -> str:
    username = (settings.bot_username or "membership_bot").lstrip("@")
    return f"https://t.me/{username}?start={gift_activation_token(gift)}"


def gift_expires_at(gift: Gift) -> datetime:
    return _created_at(gift) + timedelta(days=max(1, int(settings.gift_activation_ttl_days)))


def build_paid_gift_activation_message(
    payment: Payment,
    plan: Plan | None,
    gift: Gift,
) -> str:
    plan_name = getattr(plan, "name", None) or "выбранный тариф"
    return "\n".join(
        [
            "<b>Подарочная подписка оплачена.</b>",
            f"Тариф: <b>{plan_name}</b>",
            f"Сумма: <b>{_money(payment)}</b>",
            "",
            "Отправьте получателю эту одноразовую ссылку активации:",
            gift_activation_url(gift),
            "",
            f"Ссылка действует до <b>{gift_expires_at(gift).strftime('%Y-%m-%d')}</b> "
            "и сработает только один раз.",
            "Скидки и партнёрские начисления на подарки не применяются.",
        ]
    )


async def redeem_gift_token(
    session: AsyncSession,
    token: str,
    user: User,
    *,
    bot: Bot | None = None,
    now: datetime | None = None,
) -> GiftRedemptionResult:
    now = _aware(now or utcnow())
    gift_id = _gift_id_from_token(token)
    if gift_id is None:
        return GiftRedemptionResult("invalid", error="bad_token")

    gift = (
        await session.execute(select(Gift).where(Gift.id == gift_id).with_for_update())
    ).scalar_one_or_none()
    if gift is None or not _token_matches(gift, token):
        return GiftRedemptionResult("invalid", error="bad_token")
    if gift.redeemed_at is not None or gift.receiver_id is not None:
        return GiftRedemptionResult("already_redeemed", gift=gift)
    if now >= gift_expires_at(gift):
        return GiftRedemptionResult("expired", gift=gift)

    payment = None
    if gift.payment_id is not None:
        payment = (
            await session.execute(
                select(Payment).where(Payment.id == gift.payment_id).with_for_update()
            )
        ).scalar_one_or_none()
    if payment is None or payment.status != "succeeded" or payment.approved_at is None:
        return GiftRedemptionResult("payment_pending", gift=gift)

    plan = (
        await session.execute(select(Plan).where(Plan.id == gift.plan_id))
    ).scalar_one_or_none()
    if plan is None:
        return GiftRedemptionResult("invalid", gift=gift, error="plan_missing")

    invite = None
    if bot is not None:
        invite_result = await create_invite_links(bot, name=f"membership_saas gift#{gift.id}")
        invite = invite_result.storage_text
        if not invite_result.all_success:
            _append_payment_note(
                payment,
                "[gift telegram access grant issue] "
                + (invite_result.error_summary or "no Telegram access links created"),
            )

    subscription = await create_or_extend_subscription(
        session,
        user=user,
        plan=plan,
        source="gift",
        invite_link=invite,
        provider=None,
        provider_subscription_id=None,
        provider_status=None,
        # A gift always grants its full duration from redemption (or after the
        # recipient's current paid-through date). Provider billing dates belong
        # to the buyer's payment and must never shorten or erase gift time.
        current_period_start=None,
        current_period_end=None,
        cancel_at_period_end=False,
    )
    gift.receiver_id = user.id
    gift.redeemed_at = now
    payment.gift_recipient_id = user.id

    await dispatch(
        session,
        "subscription.activated",
        {
            "subscription_id": subscription.id,
            "user_tg_id": user.tg_id,
            "plan": plan.code,
            "expires_at": subscription.expires_at.isoformat(),
            "provider": "gift",
            "provider_status": None,
        },
    )
    return GiftRedemptionResult(
        "redeemed",
        gift=gift,
        subscription=subscription,
        invite_link=invite,
    )


def _is_unclaimed_gift_payment(payment: Payment) -> bool:
    return bool(getattr(payment, "is_gift", False)) and getattr(payment, "gift_recipient_id", None) is None


def _gift_id_from_token(token: str) -> int | None:
    if not token.startswith(GIFT_TOKEN_PREFIX):
        return None
    parts = token.removeprefix(GIFT_TOKEN_PREFIX).split("_", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0])


def _token_matches(gift: Gift, token: str) -> bool:
    expected = gift_activation_token(gift)
    return hmac.compare_digest(expected, token)


def _signature(gift: Gift) -> str:
    msg = f"{gift.id}:{_created_at(gift).isoformat()}".encode()
    secret = (settings.jwt_secret or settings.bot_token or settings.app_name).encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def _created_at(gift: Gift) -> datetime:
    return _aware(getattr(gift, "created_at", None) or utcnow())


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _money(payment: Payment) -> str:
    amount = getattr(payment, "amount", None)
    currency = str(getattr(payment, "currency", None) or "USD").upper()
    return f"${amount}" if currency == "USD" else f"{amount} {currency}"


def _append_payment_note(payment: Payment, note: str) -> None:
    current = (getattr(payment, "note", None) or "").rstrip()
    payment.note = f"{current}\n{note}".strip() if current else note
