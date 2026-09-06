"""Billing-event notification templates and safe Telegram delivery.

These templates are the one member-facing surface that both processes share.
The bot renders them after a USDT payment; the **API** renders them from the
Stripe and Lava webhooks and from admin approval in the panel, building its own
`Bot` from the token to deliver them (`api/routers/webhooks_in.py`,
`api/routers/payments.py`). That second path is why GK-443's pre-launch hold had
to reach in here: the hold was written inside the bot process, so an inbound
provider webhook could DM a member «продлите подписку через /subscribe» at the
exact moment the bot itself answered «Бот сейчас в настройке» (GK-459).
"""
from __future__ import annotations

import html
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Plan, User
from app.services.gifts import (
    build_paid_gift_activation_message,
    ensure_paid_gift_activation,
)
from app.services.notifications import send_message

logger = logging.getLogger(__name__)

# Internal sentinel for "no date/amount available"; shown to users as a dash and
# compared by identity in the failed/cancelled templates.
_UNKNOWN = "—"
_PLAN_LABELS_BY_CODE = {
    "1m": "1 месяц",
    "monthly": "1 месяц",
    "6m": "6 месяцев",
    "12m": "12 месяцев",
    "annual": "12 месяцев",
}
_PLAN_LABELS_BY_ENGLISH_NAME = {
    "monthly access": "1 месяц",
    "1 month": "1 месяц",
    "one month": "1 месяц",
    "six month access": "6 месяцев",
    "6 months": "6 месяцев",
    "six months": "6 месяцев",
    "annual access": "12 месяцев",
    "12 months": "12 месяцев",
    "yearly access": "12 месяцев",
}

#: GK-459: what a held message says instead of naming a bot command.
#:
#: While GK-443's hold is on, `/subscribe`, `/cabinet` and `/support` are not
#: merely unhelpful — the handlers that serve them are **absent from the
#: dispatcher**, so every one of them resolves to the заглушка. A billing DM
#: that names one is an instruction to a wall, and this is the sentence that
#: заглушка ends on, word for word, so a member who taps a command and a member
#: who gets a webhook DM are sent to the same place in the same words.
#:
#: Hardcoded rather than interpolated from `settings.support_contact`, for the
#: same reason `HOLD_MESSAGE` and `PAYMENT_HELP_LINE` hardcode it: this is
#: client-approved copy, and a settings value can drift into rendering a handle
#: Grant never approved. `test_the_held_tail_matches_the_one_the_hold_hands_out`
#: binds this constant to `HOLD_MESSAGE` so the two cannot drift apart.
HOLD_HELP_LINE = "Если нужна помощь — @GKcurators"


def prelaunch_hold_applies_to(tg_id: int | None) -> bool:
    """GK-459: is GK-443's pre-launch hold in force for *this* recipient?

    Read at call time rather than import time, so the answer follows the
    deployment instead of the moment the process happened to start — the same
    reason `app/bot/tasks.py::_prelaunch_hold` reads it that way.

    Allowlist-aware, because GK-446's named ids are handed the real bot in order
    to check the finished texts on a live flow; giving them the held copy would
    check the wrong thing. `tg_id is None` means the message could not be
    attributed to a person, and that lands on the hold — the same safe direction
    GK-443 chose for events it cannot attribute.

    **This suppresses the sell, not the notification.** A member whose card was
    charged is still told, in full, with the amount and the paid-through date.
    The bot's scheduled jobs could be skipped outright (GK-443) because they run
    again on the next tick; a provider webhook fires once, and silence there is
    the GK-444 outcome — charged and told nothing — which is worse than the
    defect this fixes.
    """
    settings = get_settings()
    if not settings.enable_prelaunch_hold:
        return False
    if tg_id is None:
        return True
    return tg_id not in settings.prelaunch_hold_allowlist_ids


async def notify_payment_succeeded(
    session: Any,
    payment: Any,
    subscription: Any | None,
) -> bool:
    """Notify the entitled user after a successful initial, renewal, or gift payment."""
    event = _success_event_name(payment)
    try:
        if _is_unclaimed_gift_payment(payment):
            gift = await ensure_paid_gift_activation(session, payment)
            user, plan = await _load_user_plan(
                session,
                user_id=getattr(payment, "user_id", None),
                plan_id=getattr(payment, "plan_id", None),
            )
            if gift is None or user is None:
                logger.warning(
                    "billing notification %s skipped: activation gift/buyer missing payment_id=%s",
                    event,
                    getattr(payment, "id", None),
                )
                return False
            text = build_paid_gift_activation_message(payment, plan, gift)
            return await _safe_send(
                user.tg_id,
                text,
                event=event,
                payment_id=getattr(payment, "id", None),
                user_id=getattr(user, "id", None),
                gift_id=getattr(gift, "id", None),
            )
        if subscription is None:
            logger.warning(
                "billing notification %s skipped: subscription missing payment_id=%s",
                event,
                getattr(payment, "id", None),
            )
            return False
        recipient_id = _payment_recipient_id(payment)
        user, plan = await _load_user_plan(
            session,
            user_id=recipient_id,
            plan_id=getattr(payment, "plan_id", None),
        )
        if user is None:
            logger.warning(
                "billing notification %s skipped: user not found payment_id=%s user_id=%s",
                event,
                getattr(payment, "id", None),
                recipient_id,
            )
            return False
        text = build_payment_succeeded_message(
            payment,
            plan,
            subscription,
            hold=prelaunch_hold_applies_to(user.tg_id),
        )
        return await _safe_send(
            user.tg_id,
            text,
            event=event,
            payment_id=getattr(payment, "id", None),
            user_id=getattr(user, "id", None),
        )
    except Exception:
        logger.exception(
            "billing notification %s failed; continuing billing flow payment_id=%s",
            event,
            getattr(payment, "id", None),
        )
        return False


async def notify_payment_failed(
    session: Any,
    *,
    payment: Any | None = None,
    subscription: Any | None = None,
    provider: str | None = None,
) -> bool:
    """Notify a user that a provider renewal/payment failed and may be in grace."""
    try:
        user_id = _payment_recipient_id(payment) if payment is not None else getattr(subscription, "user_id", None)
        plan_id = getattr(payment, "plan_id", None) or getattr(subscription, "plan_id", None)
        user, plan = await _load_user_plan(session, user_id=user_id, plan_id=plan_id)
        if user is None:
            logger.warning(
                "billing notification payment_failed skipped: user not found payment_id=%s subscription_id=%s user_id=%s",
                getattr(payment, "id", None),
                getattr(subscription, "id", None),
                user_id,
            )
            return False
        text = build_payment_failed_message(
            payment,
            plan,
            subscription,
            provider=provider,
            hold=prelaunch_hold_applies_to(user.tg_id),
        )
        return await _safe_send(
            user.tg_id,
            text,
            event="payment_failed",
            payment_id=getattr(payment, "id", None),
            subscription_id=getattr(subscription, "id", None),
            user_id=getattr(user, "id", None),
        )
    except Exception:
        logger.exception(
            "billing notification payment_failed failed; continuing billing flow payment_id=%s subscription_id=%s",
            getattr(payment, "id", None),
            getattr(subscription, "id", None),
        )
        return False


async def notify_subscription_cancelled(
    session: Any,
    subscription: Any,
    *,
    provider: str | None = None,
) -> bool:
    """Notify a user that the provider-side subscription was cancelled."""
    try:
        user, plan = await _load_user_plan(
            session,
            user_id=getattr(subscription, "user_id", None),
            plan_id=getattr(subscription, "plan_id", None),
        )
        if user is None:
            logger.warning(
                "billing notification subscription_cancelled skipped: user not found subscription_id=%s user_id=%s",
                getattr(subscription, "id", None),
                getattr(subscription, "user_id", None),
            )
            return False
        text = build_subscription_cancelled_message(
            subscription,
            plan,
            provider=provider,
            hold=prelaunch_hold_applies_to(user.tg_id),
        )
        return await _safe_send(
            user.tg_id,
            text,
            event="subscription_cancelled",
            subscription_id=getattr(subscription, "id", None),
            user_id=getattr(user, "id", None),
        )
    except Exception:
        logger.exception(
            "billing notification subscription_cancelled failed; continuing billing flow subscription_id=%s",
            getattr(subscription, "id", None),
        )
        return False


async def notify_archive_password_updated(
    tg_id: int,
    *,
    portal_url: str | None = None,
) -> bool:
    text = build_archive_password_updated_message(
        portal_url=portal_url,
        hold=prelaunch_hold_applies_to(tg_id),
    )
    return await _safe_send(tg_id, text, event="archive_password_updated")


async def notify_usdt_expiring(
    tg_id: int,
    expires_at: datetime,
    *,
    reply_markup: Any | None = None,
) -> bool:
    """Tell a one-time USDT subscriber to start renewal manually (GK-384)."""
    return await _safe_send(
        tg_id,
        build_usdt_expiry_reminder_message(expires_at),
        event="usdt_expiring",
        reply_markup=reply_markup,
        expires_at=expires_at.isoformat(),
    )


def build_payment_succeeded_message(
    payment: Any,
    plan: Any | None,
    subscription: Any | None,
    *,
    hold: bool = False,
) -> str:
    """`hold` is GK-459 and defaults to off deliberately.

    The one caller outside this module is the bot's USDT success path
    (`bot/handlers/subscription.py`), and that handler is in the dispatcher only
    when the hold is off or the member is on GK-446's allowlist — in both cases
    the real copy is the right copy. The API's callers pass the recipient-aware
    answer from `prelaunch_hold_applies_to`. Nothing reads global state here, so
    the builders stay pure and testable either way.
    """
    if getattr(payment, "is_gift", False):
        return build_gift_access_message(payment, plan, subscription, hold=hold)
    if getattr(payment, "is_renewal", False):
        # No branch: a renewal message names no command and offers nothing —
        # it reports a charge that already happened. The hold has no work here.
        return build_renewal_succeeded_message(payment, plan, subscription)
    return build_initial_payment_succeeded_message(payment, plan, subscription, hold=hold)


def build_initial_payment_succeeded_message(
    payment: Any,
    plan: Any | None,
    subscription: Any | None,
    *,
    hold: bool = False,
) -> str:
    lines = [
        "<b>Оплата получена.</b>",
        f"Тариф: <b>{_plan_label(plan)}</b>",
        f"Сумма: <b>{_money(payment)}</b>",
        f"Доступ до: <b>{_next_date(payment, subscription)}</b>",
    ]
    return _with_access_line(lines, subscription, hold=hold)


def build_renewal_succeeded_message(
    payment: Any,
    plan: Any | None,
    subscription: Any | None,
) -> str:
    lines = [
        "<b>Подписка продлена.</b>",
        f"Тариф: <b>{_plan_label(plan)}</b>",
        f"Сумма: <b>{_money(payment)}</b>",
        f"Следующее списание: <b>{_next_date(payment, subscription)}</b>",
    ]
    return "\n".join(lines)


def build_gift_access_message(
    payment: Any,
    plan: Any | None,
    subscription: Any | None,
    *,
    hold: bool = False,
) -> str:
    lines = [
        "<b>Вам подарили подписку membership_saas.</b>",
        f"Тариф: <b>{_plan_label(plan)}</b>",
        f"Оплачено: <b>{_money(payment)}</b>",
        f"Доступ до: <b>{_next_date(payment, subscription)}</b>",
    ]
    return _with_access_line(lines, subscription, hold=hold)


def build_payment_failed_message(
    payment: Any | None,
    plan: Any | None,
    subscription: Any | None,
    *,
    provider: str | None = None,
    hold: bool = False,
) -> str:
    provider_label = _provider_label(provider or getattr(payment, "provider", None) or getattr(subscription, "provider", None))
    lines = [
        "<b>Нужно внимание к продлению подписки.</b>",
        f"Провайдер: <b>{provider_label}</b>",
        f"Тариф: <b>{_plan_label(plan)}</b>",
    ]
    if payment is not None:
        lines.append(f"Сумма: <b>{_money(payment)}</b>")

    grace_until = _date_from(
        getattr(subscription, "grace_ends_at", None)
        or getattr(subscription, "current_period_end", None)
        or getattr(subscription, "expires_at", None)
    )
    if grace_until != _UNKNOWN:
        lines.append(
            "Доступ сохраняется до "
            f"<b>{grace_until}</b>, пока провайдер повторяет попытку оплаты или проверку."
        )
    else:
        lines.append(
            "Доступ может перейти в льготный период, пока идёт повторная попытка или проверка."
        )
    if hold:
        # GK-459: under the hold `/support` answers the заглушка, which then
        # hands out @GKcurators anyway. One hop, not two — and the message that
        # says "you may have been charged and we are not sure" is the wrong
        # place to spend a member's patience on a redirect.
        lines.append(
            "Пожалуйста, не оплачивайте дважды. "
            "Если провайдер показывает списание — напишите @GKcurators."
        )
    else:
        lines.append(
            "Пожалуйста, не оплачивайте дважды. Если провайдер показывает списание — напишите в /support."
        )
    return "\n".join(lines)


def build_subscription_cancelled_message(
    subscription: Any,
    plan: Any | None,
    *,
    provider: str | None = None,
    hold: bool = False,
) -> str:
    provider_label = _provider_label(provider or getattr(subscription, "provider", None))
    access_until = _date_from(
        getattr(subscription, "grace_ends_at", None)
        or getattr(subscription, "current_period_end", None)
        or getattr(subscription, "expires_at", None)
    )
    lines = [
        "<b>Получена отмена подписки.</b>",
        f"Провайдер: <b>{provider_label}</b>",
        f"Тариф: <b>{_plan_label(plan)}</b>",
    ]
    if access_until != _UNKNOWN:
        lines.append(f"Доступ сохраняется до <b>{access_until}</b>.")
    else:
        lines.append("Доступ будет обновлён в соответствии с оплаченным периодом и статусом провайдера.")
    if hold:
        # GK-459: this is the line the task was filed on. `/subscribe` under the
        # hold is not a poor suggestion, it is an absent handler (GK-443) — the
        # member taps it and is told the bot is under configuration. The renewal
        # offer goes entirely; every factual line above it stays.
        lines.append(HOLD_HELP_LINE)
    else:
        lines.append("Вы можете продлить подписку через /subscribe, когда будете готовы.")
    return "\n".join(lines)


def build_archive_password_updated_message(
    *,
    portal_url: str | None = None,
    hold: bool = False,
) -> str:
    lines = [
        "<b>Данные доступа к архиву обновлены.</b>",
        "Используйте портал membership_saas для актуального доступа к архиву. Старые пароли или ссылки могут перестать работать.",
    ]
    if portal_url:
        lines.append(f"Портал: {_escape(portal_url)}")
    elif hold:
        lines.append(HOLD_HELP_LINE)
    else:
        lines.append("Откройте /cabinet, чтобы получить ссылку на портал.")
    return "\n".join(lines)


def build_usdt_expiry_reminder_message(expires_at: datetime) -> str:
    return "\n".join(
        [
            f"<b>Подписка USDT оплачена до {_date_from(expires_at)}.</b>",
            "Автоматического списания не будет.",
            "Чтобы сохранить доступ, продление нужно запустить и оплатить вручную.",
        ]
    )


async def _load_user_plan(
    session: Any,
    *,
    user_id: int | None,
    plan_id: int | None,
) -> tuple[Any | None, Any | None]:
    user = None
    plan = None
    if user_id is not None:
        user = await _scalar_one_or_none(session, select(User).where(User.id == user_id))
    if plan_id is not None:
        plan = await _scalar_one_or_none(session, select(Plan).where(Plan.id == plan_id))
    return user, plan


async def _scalar_one_or_none(session: Any, query: Any) -> Any | None:
    return (await session.execute(query)).scalar_one_or_none()


async def _safe_send(
    tg_id: int,
    text: str,
    *,
    event: str,
    reply_markup: Any | None = None,
    **context: Any,
) -> bool:
    try:
        if reply_markup is None:
            delivered = await send_message(tg_id, text)
        else:
            delivered = await send_message(tg_id, text, reply_markup=reply_markup)
    except Exception:
        logger.exception(
            "billing notification %s send raised; continuing billing flow context=%s",
            event,
            _compact_context(context),
        )
        return False
    if not delivered:
        logger.warning(
            "billing notification %s delivery failed context=%s",
            event,
            _compact_context({**context, "tg_id": tg_id}),
        )
    return delivered


def _payment_recipient_id(payment: Any | None) -> int | None:
    if payment is None:
        return None
    if getattr(payment, "is_gift", False):
        return getattr(payment, "gift_recipient_id", None)
    return getattr(payment, "user_id", None)


def _success_event_name(payment: Any) -> str:
    if _is_unclaimed_gift_payment(payment):
        return "gift_activation"
    if getattr(payment, "is_gift", False):
        return "gift_access"
    if getattr(payment, "is_renewal", False):
        return "renewal_succeeded"
    return "payment_succeeded"


def _with_access_line(lines: list[str], subscription: Any | None, *, hold: bool = False) -> str:
    invite_link = getattr(subscription, "invite_link", None)
    lines.extend(["", "Доступ включает закрытый канал сообщества и чат практики."])
    if invite_link:
        # The links work under the hold — fulfilment is not held, only the sell
        # is (GK-459). This is the normal outcome of a successful payment.
        lines.extend(["Ссылки для входа:", _escape(invite_link)])
    elif hold:
        lines.append(HOLD_HELP_LINE)
    else:
        lines.append("Откройте /cabinet, чтобы увидеть подписку и доступ к архиву.")
    return "\n".join(lines)


def _is_unclaimed_gift_payment(payment: Any | None) -> bool:
    return bool(getattr(payment, "is_gift", False)) and getattr(payment, "gift_recipient_id", None) is None


def _plan_label(plan: Any | None) -> str:
    name = str(getattr(plan, "name", "") or "").strip() if plan is not None else ""
    if name:
        label = _PLAN_LABELS_BY_ENGLISH_NAME.get(name.lower())
        return _escape(label or name)
    code = str(getattr(plan, "code", "") or "").strip().lower() if plan is not None else ""
    return _escape(_PLAN_LABELS_BY_CODE.get(code, code or "выбранный тариф"))


def _provider_label(provider: str | None) -> str:
    labels = {
        "stripe": "Stripe",
        "lava": "Lava",
        "usdt": "USDT",
        "manual": "Manual",
        "zelle": "Zelle",
    }
    return labels.get(str(provider or "").lower(), _escape(provider or "provider"))


def _money(payment: Any | None) -> str:
    if payment is None:
        return _UNKNOWN
    raw_amount = getattr(payment, "amount", None)
    currency = str(getattr(payment, "currency", None) or "USD").upper()
    try:
        amount = Decimal(str(raw_amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return _escape(f"{raw_amount} {currency}".strip())
    if currency == "USD":
        return f"${amount}"
    return _escape(f"{amount} {currency}")


def _next_date(payment: Any | None, subscription: Any | None) -> str:
    return _date_from(
        getattr(payment, "billing_period_end", None)
        or getattr(subscription, "current_period_end", None)
        or getattr(subscription, "expires_at", None)
    )


def _date_from(value: Any) -> str:
    if value is None:
        return _UNKNOWN
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return _escape(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d")


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if value is not None}
