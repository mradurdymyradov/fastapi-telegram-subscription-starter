"""Dispatch autorenew cancellation to the payment provider (GK-377).

Before this module, "Отменить автопродление" only set the local
``cancel_at_period_end`` flag and dropped a note into the general support feed
while telling the member an administrator would stop the charge. Nothing ever
called Stripe or Lava, and nobody worked the feed — two curators kept being
charged on personal cards after asking to stop.

Two rules follow from that and are worth keeping:

1. ``cancel_at_period_end`` means **the provider will not charge again**. It is
   set when a provider confirms, never merely because a member asked. That is
   what makes webhook reconciliation unambiguous — see ``effective_cancel_state``.
2. When we cannot prove the charge is stopped, the member is told exactly that.
   A cancellation we only wrote down is not a cancellation.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, Subscription, SupportMessage, User, utcnow
from app.observability import send_ops_alert
from app.payments.lava_provider import (
    LavaAPIError,
    LavaCancellationUnavailable,
    LavaProvider,
)
from app.payments.stripe_provider import StripeCancellationError, StripeProvider

logger = logging.getLogger(__name__)

#: A cancellation is on record but the provider has not confirmed it.
CANCEL_REQUESTED = "requested"
#: The provider confirmed no further charge will be made.
CANCEL_PROVIDER_CONFIRMED = "provider_confirmed"
#: The provider could not be reached/asked. A human must stop the charge.
CANCEL_MANUAL_REQUIRED = "manual_required"

_PROVIDER_CANCELLED_STATUSES = {"cancelled", "canceled"}
_API_CANCELLABLE_PROVIDERS = {"stripe", "lava"}
_BUYER_EMAIL_RE = re.compile(r"buyer_email=(\S+)")


@dataclass(frozen=True)
class CancellationOutcome:
    """What actually happened, so callers can speak accurately to the member."""

    state: str
    provider: str | None
    detail: str | None = None
    #: True when the same cancellation was already on record before this call
    #: (duplicate button press, retry after a timeout).
    already_recorded: bool = False

    @property
    def provider_confirmed(self) -> bool:
        return self.state == CANCEL_PROVIDER_CONFIRMED

    @property
    def needs_manual_action(self) -> bool:
        return self.state == CANCEL_MANUAL_REQUIRED


def provider_of(sub: Subscription | None) -> str | None:
    raw = getattr(sub, "provider", None) or getattr(sub, "source", None)
    return str(raw).lower() if raw else None


def effective_cancel_state(sub: Subscription | None) -> str | None:
    """Our stored dispatch record, reconciled against provider-webhook truth.

    Provider webhooks are the authority: ``webhooks_in.py`` writes
    ``provider_status`` and ``cancel_at_period_end`` straight off the provider
    payload. Deriving the state here (rather than storing a value the webhook
    would have to remember to update) is what makes reconciliation idempotent
    and lets an admin who cancels a Lava contract by hand close the manual-queue
    item automatically — the incoming ``subscription.cancelled`` webhook is
    enough, with no extra bookkeeping.

    Returns None when no cancellation is on record.
    """
    if str(getattr(sub, "provider_status", None) or "").lower() in _PROVIDER_CANCELLED_STATUSES:
        return CANCEL_PROVIDER_CONFIRMED
    if getattr(sub, "cancel_at_period_end", False):
        # Only a provider confirmation sets this flag (see module docstring), so
        # its presence is proof regardless of what we recorded locally.
        return CANCEL_PROVIDER_CONFIRMED
    return getattr(sub, "cancel_state", None)


def has_cancellation_on_record(sub: Subscription | None) -> bool:
    return effective_cancel_state(sub) is not None


def needs_manual_cancellation(sub: Subscription | None) -> bool:
    """True for the admin queue: asked to cancel, provider not confirmed, open."""
    if effective_cancel_state(sub) != CANCEL_MANUAL_REQUIRED:
        return False
    return getattr(sub, "cancel_resolved_at", None) is None


async def request_autorenew_cancellation(
    session: AsyncSession,
    sub: Subscription,
    *,
    user: User,
) -> CancellationOutcome:
    """Ask the provider to stop future charges; fall back to a manual queue.

    Idempotent: once the provider has confirmed, repeated calls are no-ops that
    report the confirmed state instead of calling the provider again.
    """
    provider = provider_of(sub)
    current = effective_cancel_state(sub)

    if current == CANCEL_PROVIDER_CONFIRMED:
        return CancellationOutcome(
            state=CANCEL_PROVIDER_CONFIRMED,
            provider=provider,
            already_recorded=True,
        )

    already_recorded = current is not None
    contract_id = getattr(sub, "provider_subscription_id", None)
    # Lava requires the buyer email alongside the contract id, and it is the
    # same identifier a human needs for the manual queue — so load it once,
    # whichever way this goes.
    payment = await _latest_provider_payment(session, sub, provider)
    detail = await _dispatch_to_provider(
        provider,
        contract_id,
        email=buyer_email_from_payment(payment),
    )

    now = utcnow()
    if getattr(sub, "cancel_requested_at", None) is None:
        sub.cancel_requested_at = now

    if detail is None:
        sub.cancel_state = CANCEL_PROVIDER_CONFIRMED
        sub.cancel_confirmed_at = now
        sub.cancel_failure_reason = None
        # Provider truth: it will not charge again. Access still runs to the
        # paid-through date; the normal expiry job revokes it afterwards.
        sub.cancel_at_period_end = True
        logger.info(
            "autorenew cancellation confirmed provider=%s subscription_id=%s",
            provider,
            getattr(sub, "id", None),
        )
        return CancellationOutcome(
            state=CANCEL_PROVIDER_CONFIRMED,
            provider=provider,
            already_recorded=already_recorded,
        )

    sub.cancel_state = CANCEL_MANUAL_REQUIRED
    sub.cancel_failure_reason = detail[:500]
    logger.warning(
        "autorenew cancellation needs manual action provider=%s subscription_id=%s reason=%s",
        provider,
        getattr(sub, "id", None),
        detail,
    )
    if not already_recorded:
        await _record_manual_queue_item(
            session,
            sub,
            user=user,
            provider=provider,
            reason=detail,
            payment=payment,
        )
    # GK-433: the row and the support-feed entry were both written correctly and
    # reached nobody. One member sat in this state for 13 days with a renewal
    # date approaching, because seeing it required somebody to open the admin
    # panel and notice. Push it instead of waiting to be looked at.
    await _alert_manual_cancellation_required(
        sub,
        user=user,
        provider=provider,
        reason=detail,
        payment=payment,
    )
    return CancellationOutcome(
        state=CANCEL_MANUAL_REQUIRED,
        provider=provider,
        detail=detail,
        already_recorded=already_recorded,
    )


async def _dispatch_to_provider(
    provider: str | None,
    contract_id: str | None,
    *,
    email: str | None = None,
) -> str | None:
    """Return None on provider-confirmed success, else why it must be manual."""
    if provider not in _API_CANCELLABLE_PROVIDERS:
        return f"provider {provider or 'unknown'} has no autorenew cancellation API"

    try:
        if provider == "stripe":
            await StripeProvider.cancel_autorenew(contract_id)
        else:
            await LavaProvider.cancel_autorenew(contract_id, email=email)
    except (LavaCancellationUnavailable, LavaAPIError, StripeCancellationError) as exc:
        return str(exc) or exc.__class__.__name__
    except Exception as exc:  # never let a provider SDK surprise strand the request
        logger.exception("unexpected autorenew cancellation failure provider=%s", provider)
        return f"unexpected {exc.__class__.__name__}: {exc}"
    return None


async def _record_manual_queue_item(
    session: AsyncSession,
    sub: Subscription,
    *,
    user: User,
    provider: str | None,
    reason: str,
    payment: Payment | None,
) -> None:
    """Write the support-feed entry that a human works from.

    The actionable queue is the subscription row itself (``cancel_state`` +
    ``cancel_resolved_at``); this message keeps the request visible in the
    member's support history and carries the identifiers needed to find the
    contract in the provider dashboard — buyer email and payment id, because
    for RUB there is frequently no contract id to search by.
    """
    identifiers = [
        f"subscription_id={getattr(sub, 'id', None)}",
        f"provider={provider}",
        f"provider_subscription_id={getattr(sub, 'provider_subscription_id', None)}",
        f"payment_id={getattr(payment, 'id', None)}",
        f"buyer_email={buyer_email_from_payment(payment) or 'неизвестен'}",
        f"tg_id={getattr(user, 'tg_id', None)}",
    ]
    session.add(
        SupportMessage(
            user_id=user.id,
            role="user",
            content=(
                "ТРЕБУЕТСЯ РУЧНАЯ ОТМЕНА АВТОПРОДЛЕНИЯ. "
                "Отменить списание через API не удалось, подписку нужно остановить "
                "в панели провайдера вручную.\n"
                f"Причина: {reason}\n" + ", ".join(identifiers)
            ),
        )
    )


async def _latest_provider_payment(
    session: AsyncSession,
    sub: Subscription,
    provider: str | None,
) -> Payment | None:
    query = (
        select(Payment)
        .where(Payment.user_id == sub.user_id)
        .order_by(Payment.id.desc())
        .limit(1)
    )
    if provider:
        query = query.where(Payment.provider == provider)
    try:
        return (await session.execute(query)).scalar_one_or_none()
    except Exception:  # a missing payment must not block recording the request
        logger.exception("could not load payment for cancellation queue sub=%s", getattr(sub, "id", None))
        return None


def buyer_email_from_payment(payment: Payment | None) -> str | None:
    """Lava buyer email is appended to the payment note at checkout time."""
    match = _BUYER_EMAIL_RE.search(getattr(payment, "note", None) or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# GK-433: making the queue visible without anyone having to look
#
# `manual_required` was being set correctly, written to the support feed, and
# surfaced in the admin panel — and still reached nobody, because all three are
# pull, not push. sub#11 sat in it from 24.07 to 09.08 with a renewal on 24.08.
# What follows is the push half: one alert when a row enters the state, and a
# daily reminder for as long as it stays there. Both carry the only number that
# decides urgency — days until the card is charged again.
# ---------------------------------------------------------------------------

#: How long before the next charge a queue item stops being paperwork.
MANUAL_CANCELLATION_URGENT_DAYS = 7


def open_manual_cancellation_filters() -> list:
    """The definition of "still needs a human", in exactly one place.

    The admin queue endpoint, the dashboard counter and the daily alert all
    build on this. A count that disagrees with the list it points at teaches
    people to distrust both, and this is a queue whose whole value is being
    trusted enough to be worked.
    """
    return [
        Subscription.cancel_state == CANCEL_MANUAL_REQUIRED,
        Subscription.cancel_resolved_at.is_(None),
        # A provider webhook may have confirmed the cancellation after we queued
        # it (an admin cancelling in the Lava dashboard, say). Those leave the
        # queue on their own — see `effective_cancel_state`.
        Subscription.cancel_at_period_end.is_(False),
        or_(
            Subscription.provider_status.is_(None),
            Subscription.provider_status.not_in(list(_PROVIDER_CANCELLED_STATUSES)),
        ),
    ]


def open_manual_cancellations_query():
    """Open queue rows, soonest charge first — the order urgency actually has."""
    return (
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(*open_manual_cancellation_filters())
        .order_by(Subscription.expires_at.asc().nulls_last())
    )


async def open_manual_cancellations(session: AsyncSession) -> list[tuple[Subscription, User]]:
    return list((await session.execute(open_manual_cancellations_query())).all())


def days_until(when: datetime | None, *, now: datetime | None = None) -> int | None:
    if when is None:
        return None
    reference = now or utcnow()
    if when.tzinfo is None:
        when = when.replace(tzinfo=reference.tzinfo)
    return (when - reference).days


def _member_label(user: User | None) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{html.escape(str(username))}"
    return f"user #{getattr(user, 'id', '?')} (tg {getattr(user, 'tg_id', '?')})"


def _charge_line(sub: Subscription, *, now: datetime | None = None) -> str:
    expires_at = getattr(sub, "expires_at", None)
    if expires_at is None:
        return "дата следующего списания неизвестна"
    left = days_until(expires_at, now=now)
    stamp = expires_at.strftime("%d.%m.%Y")
    if left is None:
        return f"следующее списание {stamp}"
    if left < 0:
        return f"дата списания {stamp} уже прошла"
    return f"следующее списание {stamp} — через {left} дн."


def manual_cancellation_alert_text(
    sub: Subscription,
    *,
    user: User,
    provider: str | None,
    reason: str,
    payment: Payment | None = None,
    now: datetime | None = None,
) -> str:
    """The message sent the moment a cancellation cannot be confirmed."""
    email = buyer_email_from_payment(payment)
    return (
        "ТРЕБУЕТСЯ РУЧНАЯ ОТМЕНА АВТОПРОДЛЕНИЯ\n"
        f"{_member_label(user)}, {_charge_line(sub, now=now)}.\n\n"
        f"Провайдер: <code>{html.escape(str(provider or 'неизвестен'))}</code>\n"
        f"Контракт: <code>{html.escape(str(getattr(sub, 'provider_subscription_id', None) or 'нет id — искать по email'))}</code>\n"
        f"Email покупателя: <code>{html.escape(email or 'неизвестен')}</code>\n"
        f"Причина: {html.escape(reason[:200])}\n\n"
        f"Остановить списание нужно вручную в панели провайдера, "
        f"затем отметить в админке: Подписки → «Отменено вручную» (subscription_id={getattr(sub, 'id', None)})."
    )


def manual_cancellation_digest_text(
    rows: list[tuple[Subscription, User]],
    *,
    now: datetime | None = None,
    max_listed: int = 10,
) -> str:
    """The daily reminder. Sent only while the queue is non-empty."""
    reference = now or utcnow()
    urgent = [
        (sub, user)
        for sub, user in rows
        if (left := days_until(getattr(sub, "expires_at", None), now=reference)) is not None
        and left <= MANUAL_CANCELLATION_URGENT_DAYS
    ]
    header = (
        f"Ручная отмена автопродления: {len(rows)} в очереди"
        + (f", из них {len(urgent)} спишутся в ближайшие {MANUAL_CANCELLATION_URGENT_DAYS} дн." if urgent else "")
    )
    lines = [
        f"• #{getattr(sub, 'id', '?')} {_member_label(user)} — {_charge_line(sub, now=reference)}"
        for sub, user in rows[:max_listed]
    ]
    if len(rows) > max_listed:
        lines.append(f"• …и ещё {len(rows) - max_listed}")
    return (
        f"{header}\n\n"
        + "\n".join(lines)
        + "\n\nПолный список и кнопка «Отменено вручную»: админка → Подписки."
    )


async def _alert_manual_cancellation_required(
    sub: Subscription,
    *,
    user: User,
    provider: str | None,
    reason: str,
    payment: Payment | None,
) -> None:
    """Best-effort — a failed alert must never fail the member's request."""
    try:
        await send_ops_alert(
            manual_cancellation_alert_text(
                sub, user=user, provider=provider, reason=reason, payment=payment
            ),
            # Per subscription, so a member pressing the button twice does not
            # send it twice; the daily digest is what keeps a stale item loud.
            key=f"manual_cancel_required:{getattr(sub, 'id', None)}",
            rate_limit_seconds=12 * 3600,
            severity="error",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "could not alert ops about manual cancellation sub=%s", getattr(sub, "id", None)
        )
