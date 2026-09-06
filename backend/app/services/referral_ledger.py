import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, parse_fx_rates_to_usd
from app.db.models import (
    Payment,
    Referral,
    ReferralAdjustment,
    ReferralCommission,
    ReferralPayoutBatch,
    User,
    utcnow,
)
from app.observability.alerts import send_ops_alert

logger = logging.getLogger(__name__)

COMMISSION_RATE = Decimal("0.20")
PAYOUT_THRESHOLD_USD = Decimal("100.00")
VESTING_MONTHS = 3
EARNING_WINDOW_MONTHS = 12

COMMISSION_PENDING = "pending"
COMMISSION_VESTED = "vested"
COMMISSION_CANCELLED = "cancelled"
COMMISSION_PAID = "paid"

PAYOUT_DRAFT = "draft"
PAYOUT_SENT = "sent"
PAYOUT_PAID = "paid"
PAYOUT_CANCELLED = "cancelled"

_MONEY = Decimal("0.01")


class PayoutTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class PartnerEarnings:
    """Money-partner commission totals for one referrer, by ledger status."""

    pending_usd: Decimal = Decimal("0")
    vested_usd: Decimal = Decimal("0")
    paid_usd: Decimal = Decimal("0")

    @property
    def accrued_usd(self) -> Decimal:
        # Everything not cancelled — what is "in the ledger" for this partner.
        return self.pending_usd + self.vested_usd + self.paid_usd

    @property
    def available_usd(self) -> Decimal:
        # Confirmed commissions not yet paid out — the partner's "available to
        # withdraw" balance. Pending (still vesting) and already-paid rows are
        # excluded. Display only; the $100 payout threshold is unchanged.
        return self.vested_usd


@dataclass(frozen=True)
class InvitedFriend:
    """One invited user (A -> B relationship) seen from the referrer side."""

    user_id: int
    username: str | None
    first_name: str | None
    joined_at: datetime
    accrued_usd: Decimal

    @property
    def has_paid(self) -> bool:
        return self.accrued_usd > 0


@dataclass(frozen=True)
class PartnerOverview:
    invited_count: int = 0
    paid_invited_count: int = 0
    earnings: PartnerEarnings = field(default_factory=PartnerEarnings)
    friends: list[InvitedFriend] = field(default_factory=list)


async def partner_earnings_summary(
    session: AsyncSession,
    referrer_id: int,
) -> PartnerEarnings:
    """Sum a referrer's commission rows by status (cancelled excluded)."""
    rows = (
        await session.execute(
            select(
                ReferralCommission.status,
                func.coalesce(func.sum(ReferralCommission.amount_usd), 0),
            )
            .where(ReferralCommission.referrer_id == referrer_id)
            .group_by(ReferralCommission.status)
        )
    ).all()
    totals = {status: _money(amount) for status, amount in rows}
    return PartnerEarnings(
        pending_usd=totals.get(COMMISSION_PENDING, Decimal("0")),
        vested_usd=totals.get(COMMISSION_VESTED, Decimal("0")),
        paid_usd=totals.get(COMMISSION_PAID, Decimal("0")),
    )


async def invited_user_count(session: AsyncSession, referrer_id: int) -> int:
    """Count everyone attributed to this referrer (paid or not)."""
    return int(
        (
            await session.execute(
                select(func.count(User.id)).where(User.referrer_id == referrer_id)
            )
        ).scalar_one()
        or 0
    )


async def paid_invited_count(session: AsyncSession, referrer_id: int) -> int:
    """Count invitees who produced at least one non-cancelled commission.

    "Оплатило" in the partner view: an invitee counts once they have any live
    commission row (created on a successful, non-gift payment). Cancelled rows
    (chargebacks) are excluded so the number matches the accrued total.
    """
    return int(
        (
            await session.execute(
                select(func.count(func.distinct(ReferralCommission.referee_id))).where(
                    ReferralCommission.referrer_id == referrer_id,
                    ReferralCommission.status != COMMISSION_CANCELLED,
                )
            )
        ).scalar_one()
        or 0
    )


async def invited_friends_with_earnings(
    session: AsyncSession,
    referrer_id: int,
    *,
    limit: int = 10,
) -> list[InvitedFriend]:
    """List invited users (A -> B) with their current accrued commission, newest first."""
    accrued = func.coalesce(
        func.sum(
            case(
                (
                    ReferralCommission.status != COMMISSION_CANCELLED,
                    ReferralCommission.amount_usd,
                ),
                else_=0,
            )
        ),
        0,
    )
    rows = (
        await session.execute(
            select(
                User.id,
                User.username,
                User.first_name,
                User.joined_at,
                accrued,
            )
            .outerjoin(
                ReferralCommission,
                (ReferralCommission.referee_id == User.id)
                & (ReferralCommission.referrer_id == referrer_id),
            )
            .where(User.referrer_id == referrer_id)
            .group_by(User.id, User.username, User.first_name, User.joined_at)
            .order_by(User.joined_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        InvitedFriend(
            user_id=uid,
            username=username,
            first_name=first_name,
            joined_at=joined_at,
            accrued_usd=_money(accrued_amount),
        )
        for uid, username, first_name, joined_at, accrued_amount in rows
    ]


async def partner_overview(
    session: AsyncSession,
    referrer_id: int,
    *,
    friends_limit: int = 10,
) -> PartnerOverview:
    """One call for the bot partner view: counts, money totals, and the A -> B list."""
    earnings = await partner_earnings_summary(session, referrer_id)
    invited = await invited_user_count(session, referrer_id)
    paid = await paid_invited_count(session, referrer_id)
    friends = await invited_friends_with_earnings(
        session, referrer_id, limit=friends_limit
    )
    return PartnerOverview(
        invited_count=invited,
        paid_invited_count=paid,
        earnings=earnings,
        friends=friends,
    )


async def record_referral_commission_intent(
    session: AsyncSession,
    referee: User,
    payment: Payment,
    *,
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
) -> ReferralCommission | None:
    """Record one commission per successful non-gift referred-user payment.

    The first payment fixes the original 12-month earning window. Before the
    invitee completes one uninterrupted three-month paid streak, every in-window
    commission remains pending at the same retention gate. Qualification vests
    the accumulated rows; later in-window payments vest immediately.
    """
    if referee.referrer_id is None or payment.is_gift:
        return None

    source_invoice_id = _source_invoice_id(payment)
    existing_commission = await _find_existing_commission(
        session,
        payment,
        source_invoice_id=source_invoice_id,
    )
    if existing_commission is not None:
        return existing_commission

    referrer = (
        await session.execute(select(User).where(User.id == referee.referrer_id))
    ).scalar_one_or_none()
    if referrer is None:
        return None

    referral = await _get_or_create_referral(session, referrer, referee, payment)
    paid_at = _payment_effective_at(payment, coverage_start=coverage_start)
    paid_through = _payment_coverage_end(
        payment,
        coverage_start=paid_at,
        coverage_end=coverage_end,
    )
    _initialize_earning_window(referral, paid_at)
    if not _inside_earning_window(referral, paid_at):
        return None

    if getattr(referral, "retention_qualified_at", None) is None:
        await _extend_retention_streak(
            session,
            referral,
            coverage_start=paid_at,
            coverage_end=paid_through,
        )

    immediately_vested = getattr(referral, "retention_qualified_at", None) is not None
    retention_gate_at = getattr(referral, "retention_gate_at", None) or vesting_at_for_start(paid_at)
    commission_status = COMMISSION_VESTED if immediately_vested else COMMISSION_PENDING
    commission_vested_at = paid_at if immediately_vested else None

    source_amount = _money(payment.amount)
    source_currency = _currency_code(getattr(payment, "currency", None))
    fx_rate = _usd_rate_for(source_currency)
    if fx_rate is None:
        # GK-457 fail-closed. Every other branch of this function decides
        # *whether* a commission exists; this one decides what it is worth, and
        # it has no way to know. Writing the rouble figure into a dollar column
        # is the defect itself, so the row is recorded at zero, with a null rate
        # marking it, and a human is told — an unpaid partner who can be made
        # whole from `source_amount` is recoverable, a payout run built on a
        # 80×-too-large number is not.
        amount_usd = Decimal("0.00")
        await _alert_unconvertible_commission(payment, source_amount, source_currency)
    else:
        amount_usd = _commission_amount(source_amount * fx_rate)

    commission = ReferralCommission(
        referral_id=referral.id,
        referrer_id=referrer.id,
        referee_id=referee.id,
        source_payment_id=payment.id,
        source_provider=payment.provider,
        source_invoice_id=source_invoice_id,
        source_provider_event_id=getattr(payment, "provider_event_id", None),
        source_amount=source_amount,
        source_currency=source_currency,
        fx_rate_to_usd=fx_rate,
        amount_usd=amount_usd,
        status=commission_status,
        vests_at=paid_at if immediately_vested else retention_gate_at,
        vested_at=commission_vested_at,
    )
    session.add(commission)
    await session.flush()

    if not immediately_vested:
        await _qualify_referral_if_ready(session, referral, now=paid_at)
    return commission


async def cancel_pending_commission_for_payment(
    session: AsyncSession,
    payment: Payment,
    *,
    reason: str,
    now: datetime | None = None,
) -> ReferralCommission | None:
    """Cancel the unqualified streak when a source payment chargebacks."""
    commission = await _find_existing_commission(
        session,
        payment,
        source_invoice_id=_source_invoice_id(payment),
    )
    if commission is None or commission.status != COMMISSION_PENDING:
        return None
    cancelled = await _cancel_pending_streak(
        session,
        commission.referral_id,
        reason=reason,
        now=now,
    )
    return commission if commission in cancelled else None


async def cancel_pending_commission_for_referee(
    session: AsyncSession,
    referee_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> list[ReferralCommission]:
    """Cancel every pending row in an invitee's pre-qualification streak."""
    referral = (
        await session.execute(
            select(Referral)
            .where(Referral.referee_id == referee_id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if referral is None:
        return []

    return await _cancel_pending_streak(
        session,
        referral.id,
        reason=reason,
        now=now,
        referral=referral,
    )


def cancel_pending_commission(
    commission: ReferralCommission | None,
    *,
    reason: str,
    now: datetime | None = None,
) -> ReferralCommission | None:
    if commission is None or commission.status != COMMISSION_PENDING:
        return None

    cancelled_at = _as_utc(now or utcnow())
    commission.status = COMMISSION_CANCELLED
    commission.cancelled_at = cancelled_at
    commission.cancellation_reason = reason[:500]
    commission.updated_at = cancelled_at
    return commission


async def adjust_commission_for_refund(
    session: AsyncSession,
    payment: Payment,
    *,
    refund_amount: Decimal,
    refunded_total: Decimal,
    payment_total: Decimal,
    fully_refunded: bool,
    reason: str,
    admin_id: int | None = None,
    now: datetime | None = None,
) -> tuple[str, Decimal]:
    """Reconcile the referral commission when its source payment is refunded.

    Returns ``(action, adjustment_usd)`` where action is one of
    ``none|cancelled|reduced|adjusted`` and ``adjustment_usd`` is the signed delta
    applied to commission value (negative when money is clawed back).

    Aligns with BLK-005 ("refund before vesting cancels commission"):

    - commission still ``pending`` and before its vest date → a full refund
      cancels it; a partial refund reduces it to 20% of the remaining
      (non-refunded) basis, cancelling if that reaches zero. The change is
      recorded as a ``ReferralAdjustment`` so the math is auditable.
    - commission already ``vested``/``paid`` → it is NOT auto-reversed; we record a negative
      ``ReferralAdjustment`` (20% of the refunded portion, capped at the
      commission value) for manual payout reconciliation.

    GK-457: ``refund_amount`` / ``refunded_total`` / ``payment_total`` all arrive
    in the *payment's* currency, so every figure derived from them is converted
    at the rate stored on the commission row before it touches ``amount_usd``.
    Using the row's own rate rather than today's setting is the point — a refund
    must undo the accrual that happened, not the one a later rate would have
    produced, or a rate change would leak value in whichever direction it moved.
    """
    now = now or utcnow()
    commission = await _find_existing_commission(
        session,
        payment,
        source_invoice_id=_source_invoice_id(payment),
    )
    if commission is None or commission.status == COMMISSION_CANCELLED:
        return "none", Decimal("0")

    rate = _stored_rate_to_usd(commission)
    previous = _money(commission.amount_usd)
    remaining_basis = _money(payment_total) - _money(refunded_total)
    if remaining_basis < 0:
        remaining_basis = Decimal("0")
    target_commission = _commission_amount(remaining_basis * rate)
    # A refund can only ever reduce a commission. Without this clamp a row held
    # at $0 for want of a rate would be *raised* to a source-currency figure by
    # a partial refund — the GK-457 defect arriving through the back door — and
    # a legacy row could be re-inflated the same way.
    if target_commission > previous:
        target_commission = previous

    if (
        commission.status == COMMISSION_PENDING
        and _optional_utc(getattr(commission, "vests_at", None)) is not None
        and _as_utc(commission.vests_at) <= _as_utc(now)
    ):
        await _qualify_commission_referral_if_ready(
            session,
            commission,
            now=_as_utc(now),
        )

    if commission.status == COMMISSION_PENDING:
        if fully_refunded or target_commission <= 0:
            cancelled = await _cancel_pending_streak(
                session,
                commission.referral_id,
                reason=reason,
                now=now,
            )
            if commission in cancelled:
                delta = _money(Decimal("0") - previous)
                _record_adjustment(session, commission, delta, reason, admin_id, "cancelled")
                return "cancelled", delta
            if commission.status == COMMISSION_PENDING:
                return "none", Decimal("0")

        if commission.status == COMMISSION_PENDING:
            commission.amount_usd = target_commission
            commission.updated_at = now
            delta = _money(target_commission - previous)
            _record_adjustment(session, commission, delta, reason, admin_id, "reduced")
            return "reduced", delta

    # Earned commission (vested/paid): record a manual adjustment for payout
    # reconciliation instead of reversing it.
    clawback = _money(
        min(
            _money(commission.amount_usd),
            _commission_amount(_money(refund_amount) * rate),
        )
    )
    if clawback <= 0:
        return "none", Decimal("0")
    delta = _money(Decimal("0") - clawback)
    _record_adjustment(session, commission, delta, reason, admin_id, "adjusted")
    return "adjusted", delta


def _record_adjustment(
    session: AsyncSession,
    commission: ReferralCommission,
    delta: Decimal,
    reason: str,
    admin_id: int | None,
    kind: str,
) -> None:
    if delta == 0:
        return
    session.add(
        ReferralAdjustment(
            commission_id=commission.id,
            amount_usd=delta,
            reason=f"refund adjustment ({kind}): {reason}"[:1000],
            created_by_admin_id=admin_id,
        )
    )


async def vest_due_commissions(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[ReferralCommission]:
    """Qualify continuous three-month streaks and vest their pending rows.

    A due date alone is insufficient: the persisted paid-through boundary must
    still cover the retention gate. Expired coverage cancels the unqualified
    streak instead of silently vesting it.
    """
    vested_at = _as_utc(now or utcnow())
    rows = await session.execute(
        select(ReferralCommission)
        .where(
            ReferralCommission.status == COMMISSION_PENDING,
            ReferralCommission.vests_at <= vested_at,
        )
        .order_by(ReferralCommission.vests_at.asc())
    )
    commissions = list(rows.scalars().all())
    if not commissions:
        return []

    referral_ids = {commission.referral_id for commission in commissions}
    referral_rows = await session.execute(
        select(Referral)
        .where(Referral.id.in_(referral_ids))
        .with_for_update()
    )
    referrals = {referral.id: referral for referral in referral_rows.scalars().all()}
    vested: list[ReferralCommission] = []
    for referral_id in referral_ids:
        referral = referrals.get(referral_id)
        if referral is None:
            continue
        pending = [
            commission
            for commission in commissions
            if commission.referral_id == referral_id
            and commission.status == COMMISSION_PENDING
        ]
        if _retention_ready(referral, vested_at):
            _mark_referral_qualified(referral, vested_at)
            _vest_commissions(pending, referral.retention_qualified_at or vested_at)
            vested.extend(pending)
            continue

        coverage_end = _optional_utc(
            getattr(referral, "retention_coverage_ends_at", None)
        )
        gate_at = _optional_utc(getattr(referral, "retention_gate_at", None))
        if gate_at is not None and coverage_end is not None and coverage_end < gate_at:
            for commission in pending:
                cancel_pending_commission(
                    commission,
                    reason="retention.lapsed_before_qualification",
                    now=vested_at,
                )
            _reset_retention_streak(referral)
    return vested


async def create_referral_payout_batch(
    session: AsyncSession,
    *,
    currency: str = "USD",
    threshold_usd: Decimal = PAYOUT_THRESHOLD_USD,
    note: str | None = None,
    now: datetime | None = None,
) -> ReferralPayoutBatch | None:
    """Create a draft payout batch for partners whose vested balance is eligible.

    The payout threshold is a per-partner floor, not a global pool: balances from
    different referrers must never be combined to make an ineligible partner
    payable. A caller may choose a higher operational threshold, but never lower
    the configured launch minimum.
    """
    threshold = max(_money(threshold_usd), PAYOUT_THRESHOLD_USD)
    rows = await session.execute(
        select(ReferralCommission)
        .where(
            ReferralCommission.status == COMMISSION_VESTED,
            ReferralCommission.payout_batch_id.is_(None),
        )
        .order_by(ReferralCommission.vested_at.asc(), ReferralCommission.id.asc())
        .with_for_update()
    )
    commissions = list(rows.scalars().all())
    totals_by_referrer: dict[int, Decimal] = {}
    for commission in commissions:
        totals_by_referrer[commission.referrer_id] = _money(
            totals_by_referrer.get(commission.referrer_id, Decimal("0"))
            + _money(commission.amount_usd)
        )
    eligible_referrers = {
        referrer_id
        for referrer_id, referrer_total in totals_by_referrer.items()
        if referrer_total >= threshold
    }
    eligible_commissions = [
        commission
        for commission in commissions
        if commission.referrer_id in eligible_referrers
    ]
    if not eligible_commissions:
        return None
    total = sum(
        (_money(commission.amount_usd) for commission in eligible_commissions),
        Decimal("0"),
    )

    batch = ReferralPayoutBatch(
        status="draft",
        currency=currency.upper(),
        threshold_amount=threshold,
        total_amount=_money(total),
        commission_count=len(eligible_commissions),
        note=note,
        created_at=now or utcnow(),
    )
    session.add(batch)
    await session.flush()

    for commission in eligible_commissions:
        commission.payout_batch_id = batch.id
        commission.updated_at = batch.created_at
    return batch


async def transition_referral_payout_batch(
    session: AsyncSession,
    batch_id: int,
    *,
    action: str,
    admin_id: int | None = None,
    tx_hash: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> ReferralPayoutBatch | None:
    """Move a manual payout batch through review states.

    Commissions become `paid` only once, when the batch enters `paid`.
    Cancelling a draft/sent batch releases its unpaid commissions back to the
    unbatched vested pool so they can be included in a later manual payout.
    """
    if action not in {PAYOUT_SENT, PAYOUT_PAID, PAYOUT_CANCELLED}:
        raise PayoutTransitionError("Unsupported payout batch action")

    batch = (
        await session.execute(
            select(ReferralPayoutBatch)
            .where(ReferralPayoutBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        return None

    current_status = batch.status
    transitioned_at = now or utcnow()
    tx_hash = (tx_hash or "").strip() or None
    note = (note or "").strip() or None

    if current_status == PAYOUT_PAID:
        raise PayoutTransitionError("Payout batch is already paid")
    if current_status == PAYOUT_CANCELLED:
        raise PayoutTransitionError("Payout batch is already cancelled")

    commissions = list(
        (
            await session.execute(
                select(ReferralCommission)
                .where(ReferralCommission.payout_batch_id == batch.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )

    if action == PAYOUT_SENT:
        if current_status != PAYOUT_DRAFT:
            raise PayoutTransitionError("Only draft payout batches can be marked sent")
        batch.status = PAYOUT_SENT
        batch.sent_at = transitioned_at

    elif action == PAYOUT_PAID:
        if current_status not in {PAYOUT_DRAFT, PAYOUT_SENT}:
            raise PayoutTransitionError("Only draft or sent payout batches can be marked paid")
        if not tx_hash and not note:
            raise PayoutTransitionError("Paid payout batches require a tx hash or note")
        if not commissions:
            raise PayoutTransitionError("Payout batch has no commissions")
        if any(commission.status == COMMISSION_PAID for commission in commissions):
            raise PayoutTransitionError("One or more commissions are already paid")
        invalid = [
            commission.id
            for commission in commissions
            if commission.status != COMMISSION_VESTED
        ]
        if invalid:
            raise PayoutTransitionError("Only vested commissions can be paid")

        batch.status = PAYOUT_PAID
        batch.sent_at = batch.sent_at or transitioned_at
        batch.paid_at = transitioned_at
        for commission in commissions:
            commission.status = COMMISSION_PAID
            commission.paid_at = transitioned_at
            commission.updated_at = transitioned_at

    else:
        if current_status not in {PAYOUT_DRAFT, PAYOUT_SENT}:
            raise PayoutTransitionError("Only draft or sent payout batches can be cancelled")
        batch.status = PAYOUT_CANCELLED
        batch.cancelled_at = transitioned_at
        for commission in commissions:
            if commission.status != COMMISSION_PAID:
                commission.payout_batch_id = None
                commission.updated_at = transitioned_at

    batch.note = _append_payout_note(
        batch.note,
        action=action,
        admin_id=admin_id,
        tx_hash=tx_hash,
        note=note,
        now=transitioned_at,
    )
    return batch


def vesting_at_for_payment(payment: Payment) -> datetime:
    return vesting_at_for_start(_payment_effective_at(payment))


def vesting_at_for_start(started_at: datetime) -> datetime:
    return _as_utc(started_at) + relativedelta(months=VESTING_MONTHS)


def earning_window_ends_at(started_at: datetime) -> datetime:
    return _as_utc(started_at) + relativedelta(months=EARNING_WINDOW_MONTHS)


def _payment_effective_at(
    payment: Payment,
    *,
    coverage_start: datetime | None = None,
) -> datetime:
    value = (
        coverage_start
        or getattr(payment, "billing_period_start", None)
        or getattr(payment, "approved_at", None)
        or getattr(payment, "created_at", None)
        or utcnow()
    )
    return _as_utc(value)


def _payment_coverage_end(
    payment: Payment,
    *,
    coverage_start: datetime,
    coverage_end: datetime | None = None,
) -> datetime:
    value = coverage_end or getattr(payment, "billing_period_end", None) or coverage_start
    normalized = _as_utc(value)
    return max(coverage_start, normalized)


def _initialize_earning_window(referral: Referral, paid_at: datetime) -> None:
    started_at = _optional_utc(getattr(referral, "partner_earning_started_at", None))
    if started_at is None:
        started_at = paid_at
        referral.partner_earning_started_at = started_at
    if getattr(referral, "partner_earning_ends_at", None) is None:
        referral.partner_earning_ends_at = earning_window_ends_at(started_at)


def _inside_earning_window(referral: Referral, paid_at: datetime) -> bool:
    started_at = _optional_utc(getattr(referral, "partner_earning_started_at", None))
    ends_at = _optional_utc(getattr(referral, "partner_earning_ends_at", None))
    return bool(
        started_at is not None
        and ends_at is not None
        and started_at <= paid_at < ends_at
    )


async def _extend_retention_streak(
    session: AsyncSession,
    referral: Referral,
    *,
    coverage_start: datetime,
    coverage_end: datetime,
) -> None:
    streak_start = _optional_utc(getattr(referral, "retention_streak_started_at", None))
    streak_end = _optional_utc(getattr(referral, "retention_coverage_ends_at", None))

    if streak_start is None or streak_end is None:
        referral.retention_streak_started_at = coverage_start
        referral.retention_coverage_ends_at = coverage_end
        referral.retention_gate_at = vesting_at_for_start(coverage_start)
        return

    if coverage_start > streak_end:
        await _cancel_pending_streak(
            session,
            referral.id,
            reason="retention.lapsed_before_next_payment",
            now=coverage_start,
            referral=referral,
        )
        if getattr(referral, "retention_qualified_at", None) is not None:
            return
        referral.retention_streak_started_at = coverage_start
        referral.retention_coverage_ends_at = coverage_end
        referral.retention_gate_at = vesting_at_for_start(coverage_start)
        return

    if coverage_end > streak_end:
        referral.retention_coverage_ends_at = coverage_end


def _retention_ready(referral: Referral, now: datetime) -> bool:
    if getattr(referral, "retention_qualified_at", None) is not None:
        return True
    gate_at = _optional_utc(getattr(referral, "retention_gate_at", None))
    coverage_end = _optional_utc(getattr(referral, "retention_coverage_ends_at", None))
    return bool(
        gate_at is not None
        and coverage_end is not None
        and now >= gate_at
        and coverage_end >= gate_at
    )


def _mark_referral_qualified(referral: Referral, qualified_at: datetime) -> None:
    if getattr(referral, "retention_qualified_at", None) is not None:
        return
    gate_at = _optional_utc(getattr(referral, "retention_gate_at", None))
    referral.retention_qualified_at = gate_at or qualified_at


def _vest_commissions(
    commissions: list[ReferralCommission],
    vested_at: datetime,
) -> None:
    for commission in commissions:
        if commission.status != COMMISSION_PENDING:
            continue
        commission.status = COMMISSION_VESTED
        commission.vested_at = vested_at
        commission.updated_at = vested_at


async def _qualify_referral_if_ready(
    session: AsyncSession,
    referral: Referral,
    *,
    now: datetime,
) -> bool:
    now = _as_utc(now)
    if not _retention_ready(referral, now):
        return False
    _mark_referral_qualified(referral, now)
    pending_rows = await session.execute(
        select(ReferralCommission)
        .where(
            ReferralCommission.referral_id == referral.id,
            ReferralCommission.status == COMMISSION_PENDING,
        )
        .order_by(ReferralCommission.created_at.asc())
        .with_for_update()
    )
    _vest_commissions(
        list(pending_rows.scalars().all()),
        referral.retention_qualified_at or now,
    )
    return True


async def _qualify_commission_referral_if_ready(
    session: AsyncSession,
    commission: ReferralCommission,
    *,
    now: datetime,
) -> bool:
    referral = (
        await session.execute(
            select(Referral)
            .where(Referral.id == commission.referral_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if referral is None:
        return False
    return await _qualify_referral_if_ready(session, referral, now=now)


async def _cancel_pending_streak(
    session: AsyncSession,
    referral_id: int,
    *,
    reason: str,
    now: datetime | None = None,
    referral: Referral | None = None,
) -> list[ReferralCommission]:
    cancelled_at = _as_utc(now or utcnow())
    if referral is None:
        referral = (
            await session.execute(
                select(Referral)
                .where(Referral.id == referral_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    if referral is None:
        return []

    pending_rows = await session.execute(
        select(ReferralCommission)
        .where(
            ReferralCommission.referral_id == referral.id,
            ReferralCommission.status == COMMISSION_PENDING,
        )
        .order_by(ReferralCommission.created_at.asc())
        .with_for_update()
    )
    pending = list(pending_rows.scalars().all())
    if _retention_ready(referral, cancelled_at):
        _mark_referral_qualified(referral, cancelled_at)
        _vest_commissions(
            pending,
            referral.retention_qualified_at or cancelled_at,
        )
        return []

    cancelled = [
        commission
        for commission in (
            cancel_pending_commission(row, reason=reason, now=cancelled_at)
            for row in pending
        )
        if commission is not None
    ]
    _reset_retention_streak(referral)
    return cancelled


def _reset_retention_streak(referral: Referral) -> None:
    referral.retention_streak_started_at = None
    referral.retention_coverage_ends_at = None
    referral.retention_gate_at = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


async def _get_or_create_referral(
    session: AsyncSession,
    referrer: User,
    referee: User,
    payment: Payment,
) -> Referral:
    referral = (
        await session.execute(
            select(Referral)
            .where(Referral.referee_id == referee.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if referral is not None:
        if referral.first_payment_id is None:
            referral.first_payment_id = payment.id
        return referral

    referral = Referral(
        referrer_id=referrer.id,
        referee_id=referee.id,
        bonus_days_granted=0,
        first_payment_id=payment.id,
    )
    session.add(referral)
    await session.flush()
    return referral


async def _find_existing_commission(
    session: AsyncSession,
    payment: Payment,
    *,
    source_invoice_id: str | None,
) -> ReferralCommission | None:
    filters = []
    if payment.id is not None:
        filters.append(ReferralCommission.source_payment_id == payment.id)
    if source_invoice_id:
        filters.append(
            (ReferralCommission.source_provider == payment.provider)
            & (ReferralCommission.source_invoice_id == source_invoice_id)
        )
    source_provider_event_id = getattr(payment, "provider_event_id", None)
    if source_provider_event_id:
        filters.append(
            (ReferralCommission.source_provider == payment.provider)
            & (
                ReferralCommission.source_provider_event_id
                == source_provider_event_id
            )
        )
    if not filters:
        return None

    return (
        await session.execute(
            select(ReferralCommission).where(or_(*filters)).limit(1)
        )
    ).scalar_one_or_none()


def _source_invoice_id(payment: Payment) -> str | None:
    return (
        getattr(payment, "stripe_invoice_id", None)
        or getattr(payment, "lava_invoice_id", None)
        or getattr(payment, "provider_event_id", None)
        or getattr(payment, "tx_hash", None)
        or getattr(payment, "external_id", None)
    )


def _commission_amount(amount_usd: object) -> Decimal:
    """20% of a **dollar** basis. Callers convert before they get here (GK-457)."""
    return _money(_money(amount_usd) * COMMISSION_RATE)


def _currency_code(value: object) -> str:
    """Normalize a provider-supplied currency to a short alphanumeric code.

    `payment.currency` reaches us from a webhook payload, so it is not ours to
    trust. It becomes a dict key, the `key=` of a rate-limited ops alert, and a
    line in that alert's text; stripping to alphanumerics costs nothing and keeps
    a provider from deciding the shape of any of the three.

    It is *not* what makes the alert safe to render — GK-451 escapes the whole
    body inside `send_ops_alert`, so nothing here needs to produce markup-free
    text. This is a narrowing of untrusted input, and would still be right if the
    alert were never sent.
    """
    text = str(value or "USD")
    code = "".join(char for char in text if char.isalnum())[:8].upper()
    return code or "USD"


def _configured_fx_rates() -> dict[str, Decimal]:
    try:
        return parse_fx_rates_to_usd(get_settings().referral_fx_rates_to_usd)
    except ValueError as exc:
        # `validate_security` refuses to boot prod on a malformed table, so this
        # is the demo/dev path — and either way a bad setting must not take down
        # the payment webhook that called us. Empty means every non-USD payment
        # routes to the fail-closed branch, which alerts rather than guessing.
        logger.error("REFERRAL_FX_RATES_TO_USD is unusable (%s); no rates loaded", exc)
        return {}


def _usd_rate_for(currency: str) -> Decimal | None:
    """USD per unit of ``currency``, or None when nothing configured says."""
    if currency == "USD":
        return Decimal("1")
    return _configured_fx_rates().get(currency)


def _stored_rate_to_usd(commission: ReferralCommission) -> Decimal:
    """The rate a commission row was accrued at, for arithmetic in its own units.

    NULL is not an error to raise on: it is every row written before GK-457 plus
    every row held at zero for want of a rate. Reading it as 1 keeps a refund
    working in the units the row was actually written in, which is what makes a
    reduction proportional; the caller's clamp stops that from ever *increasing*
    a figure.
    """
    stored = getattr(commission, "fx_rate_to_usd", None)
    if stored is None:
        return Decimal("1")
    return Decimal(str(stored))


async def _alert_unconvertible_commission(
    payment: Payment,
    amount: Decimal,
    currency: str,
) -> None:
    payment_id = getattr(payment, "id", None)
    logger.error(
        "referral commission held at $0.00: no USD rate configured for %s "
        "(payment_id=%s, amount=%s)",
        currency,
        payment_id,
        amount,
    )
    await send_ops_alert(
        # Plain text: GK-451 escapes the whole body inside `send_ops_alert`, so
        # markup here would arrive as the literal characters.
        "Реферальная комиссия начислена как $0.00 — нет курса для "
        f"{currency}.\n"
        f"payment_id={payment_id}, оплата {amount} {currency}\n"
        f"Добавьте {currency} в REFERRAL_FX_RATES_TO_USD, затем начислите "
        "партнёру вручную через корректировку.",
        key=f"referral_fx_missing:{currency}",
        rate_limit_seconds=1800,
        severity="error",
    )


def _append_payout_note(
    existing: str | None,
    *,
    action: str,
    admin_id: int | None,
    tx_hash: str | None,
    note: str | None,
    now: datetime,
) -> str | None:
    if not tx_hash and not note:
        return existing

    parts = [f"[{now.isoformat()} action={action}"]
    if admin_id is not None:
        parts.append(f"admin_id={admin_id}")
    if tx_hash:
        parts.append(f"tx_hash={tx_hash[:200]}")
    parts.append("]")
    if note:
        parts.append(note[:1000])
    line = " ".join(parts)
    return f"{(existing or '').strip()}\n{line}".strip()


def _money(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(_MONEY, rounding=ROUND_HALF_UP)
