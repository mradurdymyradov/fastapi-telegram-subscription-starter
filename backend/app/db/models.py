import logging
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="ru")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", foreign_keys="Subscription.user_id")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user", foreign_keys="Payment.user_id")


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_rub: Mapped[float] = mapped_column(Numeric(10, 2))
    price_usd: Mapped[float] = mapped_column(Numeric(10, 2))
    duration_days: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(
        SAEnum("active", "expired", "cancelled", "gifted", name="sub_status"),
        default="active",
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32), default="stripe")  # stripe|lava|zelle|usdt|gift|manual|comp
    # GK-483: this subscription exists so somebody keeps access, not because
    # anybody paid — the client's own team (Grant, Owner's two accounts, the
    # curators and moderators). Deliberately a flag beside `status`, not a
    # `sub_status` value: the row stays `active`, so invites, ban/unban and the
    # portal predicate keep behaving exactly as they do for a paying member,
    # while `active_subs`, churn and the CRM export all read this and leave the
    # row out of the paying figures.
    #
    # It also ends the expiry clock. `has_subscription_access` returns True for
    # a comp row whatever `expires_at` says, so `should_revoke_access` can never
    # be true for one and the hourly `kick_expired_job` cannot reach it. That is
    # the deliberate choice over "set the dates far out": a far-future date is a
    # silent deadline that eventually arrives and reads, in every query, exactly
    # like a paid subscription. `access_revoked_at` still wins over the flag, so
    # banning a team member removes them like anyone else.
    is_comp: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    invite_link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notified_expiring: Mapped[bool] = mapped_column(Boolean, default=False)
    # Provider lifecycle (GK-010): kept as plain strings to avoid Postgres ENUM churn.
    # provider/provider_status accept arbitrary raw values; the canonical set lives in the
    # service layer (stripe|lava|manual|usdt|gift // active|past_due|canceled|incomplete|trialing|...).
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cancellation dispatch record (GK-377). cancel_at_period_end says access
    # stops at the paid-through date; these say whether the *provider* was
    # actually told. NULL cancel_state = no cancellation requested. Canonical
    # values live in app.services.subscription_cancellation.
    cancel_state: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_resolved_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id"), nullable=True
    )
    grace_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_revoke_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_revoke_retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_revoke_attempts: Mapped[int] = mapped_column(Integer, default=0)
    access_revoke_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # GK-432: the bot gave up removing this member from Telegram. `sub#2` reached
    # 647 failed attempts because Telegram will never let a bot remove a chat
    # owner, and a row that retries forever is indistinguishable from one that is
    # about to succeed. Set means: stop retrying, a human was told once, and the
    # member is still in the channel. Entitlement is unaffected — the portal and
    # the access predicate are date-based and already say no.
    access_revoke_abandoned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    user: Mapped[User] = relationship(back_populates="subscriptions", foreign_keys=[user_id])
    plan: Mapped[Plan] = relationship()


# ---------------------------------------------------------------------------
# GK-434.2 — the billing period may not run backwards.
#
# `sub#6` on the live database has `current_period_start = 2027-01-20` and
# `current_period_end = 2026-07-28`. Nothing anywhere asserted the two were
# ordered, and three places set them independently, so any two of them
# disagreeing produced that shape:
#
#   1. `create_or_extend_subscription`'s renewal branch, which writes the *end*
#      of the window in progress into `current_period_start` when the provider
#      supplied no period of its own;
#   2. `_handle_stripe_subscription_event` and
#   3. `_handle_lava_subscription_event`, which each write the two fields under
#      separate `if … is not None` guards — a payload carrying one and not the
#      other moves one end of the interval and leaves the other where it was.
#
# The guard belongs here rather than in any of the three, because the defect is
# produced by their *combination* and a fourth writer would reintroduce it. A
# mapper-level event sees the row once, at flush, with both fields settled.
#
# It repairs rather than raises, deliberately. All three writers sit inside
# payment webhooks: raising there returns 500, the provider retries, and a
# member who has paid gets nothing — trading a reporting defect for a money and
# access defect. Entitlement never reads these fields (it reads `expires_at`
# via `has_subscription_access`), so a bad pair is genuinely cosmetic until
# something does date arithmetic on it.
#
# `current_period_start` is the field clamped, not `current_period_end`,
# because the end is consumed — billing notifications fall back to it for the
# renewal date and reconciliation compares it against the provider — while the
# start is read only by exports and snapshots. Clamping yields a zero-length
# period: visibly anomalous, arithmetically safe, and inventing nothing, since
# which of the two values is the wrong one is not knowable here.
#
# Logged at ERROR, so it reaches Sentry and is not a silent correction.
# ---------------------------------------------------------------------------

_models_logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    """Naive values are treated as UTC — comparing them raises otherwise."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalize_billing_period(_mapper, _connection, target: "Subscription") -> None:
    start, end = target.current_period_start, target.current_period_end
    if start is None or end is None:
        return
    if _as_utc(start) <= _as_utc(end):
        return

    _models_logger.error(
        "subscription %s had a billing period running backwards "
        "(current_period_start=%s > current_period_end=%s); clamping the start "
        "to the end. Some write set the two independently — see GK-434.2.",
        target.id if getattr(target, "id", None) is not None else "<new>",
        start.isoformat(),
        end.isoformat(),
    )
    target.current_period_start = end


event.listen(Subscription, "before_insert", _normalize_billing_period)
event.listen(Subscription, "before_update", _normalize_billing_period)


class Payment(Base):
    __tablename__ = "payments"
    # GK-010: a single Stripe invoice can only fulfill once. Multiple NULLs are allowed
    # because Postgres treats NULL as distinct in UNIQUE; one-off payments without a
    # stripe_invoice_id stay unconstrained.
    __table_args__ = (
        UniqueConstraint("stripe_invoice_id", name="uq_payments_stripe_invoice"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # stripe|lava|zelle|usdt
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    status: Mapped[str] = mapped_column(
        SAEnum("pending", "awaiting_review", "succeeded", "failed", "refunded", name="payment_status"),
        default="pending",
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    screenshot_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False)
    gift_recipient_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    # GK-010 provider invoice/session/event lifecycle.
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # GK-200: persisted from invoice.paid so a refund can target the charge by
    # payment intent without re-fetching the invoice from Stripe.
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    lava_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    lava_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tx_network: Mapped[str | None] = mapped_column(String(16), nullable=True)  # TRC20|ERC20
    tx_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_renewal: Mapped[bool] = mapped_column(Boolean, default=False)
    billing_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # GK-382: denormalized running total of provider-confirmed refunds. status flips to
    # 'refunded' once this reaches `amount`; a value strictly between 0 and
    # `amount` means a partial refund (status stays 'succeeded').
    refunded_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0, server_default="0")

    user: Mapped[User] = relationship(back_populates="payments", foreign_keys=[user_id])
    plan: Mapped[Plan | None] = relationship()
    refunds: Mapped[list["Refund"]] = relationship(back_populates="payment")


class Refund(Base):
    """One requested or confirmed refund against a Payment (GK-200/GK-382).

    ``Payment.refunded_amount`` is the running total of only
    ``provider_confirmed`` rows whose accounting transition was applied. Pending
    or manual-action rows reserve an amount without changing cash/access/ledger
    state. ``provider_refund_id`` holds either the provider refund id or the
    audited external reference supplied when a manual refund is confirmed.
    """
    __tablename__ = "refunds"
    __table_args__ = (
        UniqueConstraint("provider", "provider_refund_id", name="uq_refunds_provider_refund_id"),
        UniqueConstraint("request_key", name="uq_refunds_request_key"),
        CheckConstraint(
            "status IN ('requested', 'pending', 'provider_confirmed', "
            "'failed', 'manual_action_required')",
            name="ck_refunds_status",
        ),
        CheckConstraint(
            "refund_type IN ('full', 'partial')",
            name="ck_refunds_type",
        ),
        Index(
            "uq_refunds_one_active_per_payment",
            "payment_id",
            unique=True,
            postgresql_where=text(
                "status IN ('requested', 'pending', 'manual_action_required')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # stripe|lava|usdt|zelle
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    refund_type: Mapped[str] = mapped_column(String(16), default="full")  # full|partial
    status: Mapped[str] = mapped_column(String(32), default="requested", index=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True when money moves outside our system (USDT/Zelle, or Lava manual): the
    # admin asserts they sent funds back; no provider API call was made.
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What happened to the linked referral commission: none|cancelled|reduced|adjusted.
    commission_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    commission_adjustment_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    created_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    confirmed_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id"), nullable=True
    )
    confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accounting_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    payment: Mapped[Payment] = relationship(back_populates="refunds")


class PaymentProviderEvent(Base):
    """Idempotency record for webhook events from payment providers.

    One row per provider-side event id. Webhook handlers must insert here first;
    a UniqueViolation means the event has already been processed and the handler
    should return 200 without re-running fulfillment.
    """
    __tablename__ = "payment_provider_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_payment_provider_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # stripe|lava|usdt
    event_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    raw_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_errors', 'failed')",
            name="ck_reconciliation_runs_status",
        ),
        CheckConstraint(
            "triggered_by IN ('scheduler', 'admin', 'manual', 'test')",
            name="ck_reconciliation_runs_triggered_by",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    triggered_by: Mapped[str] = mapped_column(String(32), default="scheduler", index=True)
    provider_scope: Mapped[str] = mapped_column(String(32), default="all", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_count: Mapped[int] = mapped_column(Integer, default=0)
    open_items_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["ReconciliationItem"]] = relationship(back_populates="run")


class ReconciliationItem(Base):
    __tablename__ = "reconciliation_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_reconciliation_items_status",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_reconciliation_items_severity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning", index=True)
    issue_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    expected_state: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_state: Mapped[dict] = mapped_column(JSON, default=dict)
    resolve_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id"), nullable=True, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # GK-430: when this *condition* was first detected, carried across runs.
    # `created_at` is when this row was written, which for a condition re-detected
    # every night says only "last night" and hides that it has stood for months.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=text("now()"), index=True
    )

    run: Mapped[ReconciliationRun] = relationship(back_populates="items")
    resolved_by: Mapped["AdminUser | None"] = relationship()


class ReferralAttribution(Base):
    __tablename__ = "referral_attributions"
    __table_args__ = (
        UniqueConstraint("referee_id", name="uq_referral_attributions_referee"),
        CheckConstraint(
            "source IN ('telegram_deeplink', 'promo_code', 'admin', 'legacy')",
            name="ck_referral_attributions_source",
        ),
        CheckConstraint(
            "review_status IN ('clear', 'suspicious', 'dismissed')",
            name="ck_referral_attributions_review_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="telegram_deeplink", index=True)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    review_status: Mapped[str] = mapped_column(String(16), default="clear", index=True)
    suspicious_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ignored_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_ignored_referrer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    last_ignored_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_ignored_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_ignored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Referral(Base):
    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("referee_id", name="uq_referee"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referee_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    bonus_days_granted: Mapped[int] = mapped_column(Integer, default=0)
    first_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    # GK-020 partner economics. The earning window never restarts: it is anchored
    # to the first successful referred payment. Retention streak fields may reset
    # after a pre-qualification lapse/refund while the original window remains.
    partner_earning_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    partner_earning_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    retention_streak_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retention_coverage_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retention_gate_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    retention_qualified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReferralPayoutBatch(Base):
    __tablename__ = "referral_payout_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'sent', 'paid', 'cancelled')",
            name="ck_referral_payout_batches_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD", index=True)
    threshold_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=100)
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    commission_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReferralCommission(Base):
    __tablename__ = "referral_commissions"
    __table_args__ = (
        UniqueConstraint("source_payment_id", name="uq_referral_commissions_payment"),
        UniqueConstraint(
            "source_provider",
            "source_invoice_id",
            name="uq_referral_commissions_provider_invoice",
        ),
        UniqueConstraint(
            "source_provider",
            "source_provider_event_id",
            name="uq_referral_commissions_provider_event",
        ),
        CheckConstraint(
            "status IN ('pending', 'vested', 'cancelled', 'paid')",
            name="ck_referral_commissions_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id"), index=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    source_provider: Mapped[str] = mapped_column(String(32), index=True)
    source_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_amount: Mapped[float] = mapped_column(Numeric(10, 2))
    source_currency: Mapped[str] = mapped_column(String(8), default="USD")
    #: GK-457. USD per unit of ``source_currency`` at the moment this row was
    #: accrued, so ``amount_usd == source_amount * fx_rate_to_usd * 20%`` is
    #: checkable years later without knowing what the setting said that day.
    #: 1 for a dollar payment.
    #:
    #: NULL means one of two different things, and they are told apart by the
    #: row's own age, not by a flag: rows written before this column existed
    #: carry whatever `amount_usd` meant then (for a rouble payment, roubles —
    #: the defect), while a row written after it means the currency had no
    #: configured rate, so `amount_usd` was deliberately held at 0 rather than
    #: guessed. Both are "do not trust this figure without looking"; neither is
    #: silently summed into a payout, because 0 adds nothing and the legacy rows
    #: are a fixed, countable set (see the GK-457 predicate in the backlog).
    fx_rate_to_usd: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    amount_usd: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    vests_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    vested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payout_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("referral_payout_batches.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReferralAdjustment(Base):
    __tablename__ = "referral_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("referral_commissions.id"), index=True)
    amount_usd: Mapped[float] = mapped_column(Numeric(10, 2))
    reason: Mapped[str] = mapped_column(Text)
    created_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PromoCode(Base):
    """A reusable promo/discount code (GK-210).

    Beyond the per-user referral discount, a promo code is an admin-created
    coupon with its own discount, optional plan scope, usage cap, and validity
    window. ``discount_type`` is a plain VARCHAR validated in the service layer
    (CLAUDE.md enum rule). A percent code uses ``percent_off``; a fixed code uses
    ``amount_off`` denominated in ``amount_off_currency`` — a fixed code only
    discounts a checkout in the *same* currency (cross-currency is skipped, see
    ``app/services/promo.py``).

    ``referrer_user_id`` turns a code into an influencer code: redeeming it also
    writes a first-touch ``ReferralAttribution`` (source=``promo_code``) so the
    referrer earns commission through the existing ledger. Promo and referral
    discounts never stack — an applied promo replaces the referral discount.

    ``redeemed_count`` is a denormalized running total of *applied* redemptions
    (incremented at checkout creation, the same point the discount is applied to
    the Payment row). One redemption per (code, user) is enforced by a unique
    constraint on ``promo_redemptions``.
    """
    __tablename__ = "promo_codes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_promo_codes_code"),
        CheckConstraint(
            "discount_type IN ('percent', 'fixed')",
            name="ck_promo_codes_discount_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_type: Mapped[str] = mapped_column(String(16), default="percent")  # percent|fixed
    percent_off: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    amount_off: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    amount_off_currency: Mapped[str] = mapped_column(String(8), default="USD")
    # Empty list ⇒ applies to every plan; otherwise a list of Plan.code strings.
    applies_to_plan_codes: Mapped[list] = mapped_column(JSON, default=list)
    # NULL ⇒ unlimited total redemptions.
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redeemed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Influencer code: redeeming attributes the referee to this user.
    referrer_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    redemptions: Mapped[list["PromoRedemption"]] = relationship(back_populates="promo")


class PromoRedemption(Base):
    """One recorded use of a promo code by a user (GK-210).

    Created at checkout creation, in lockstep with the discount applied to the
    Payment row. The unique (promo_code_id, user_id) constraint enforces
    one-redemption-per-user and makes the "duplicate redemption" case a hard
    DB-level guarantee. ``status`` is plain VARCHAR validated in the service
    layer; ``cancelled`` redemptions are kept for audit and do not count toward
    the duplicate check.
    """
    __tablename__ = "promo_redemptions"
    __table_args__ = (
        UniqueConstraint("promo_code_id", "user_id", name="uq_promo_redemptions_code_user"),
        CheckConstraint(
            "status IN ('applied', 'cancelled')",
            name="ck_promo_redemptions_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    original_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    discount_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    final_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    promo: Mapped[PromoCode] = relationship(back_populates="redemptions")


class ReferralDiscountReservation(Base):
    """One-time referral-discount reservation for a referred user (GK-402).

    The per-user referral discount (20% off the first monthly invoice) must be
    grantable exactly once, even while several checkouts are still pending. This
    ledger row is created at *discounted-checkout creation* — never on the
    read-only price-preview path — and a partial unique index guarantees at most
    one ``active`` or ``consumed`` reservation per user:

    - ``active``:   reserved for a pending checkout; occupies the user's slot.
    - ``consumed``: the linked payment succeeded and was fulfilled — the benefit
      is spent and can never be re-granted.
    - ``released``: the checkout was abandoned/expired or rolled back; the slot is
      free to be re-reserved later.

    ``status`` is a plain VARCHAR validated in the service layer plus a CHECK (the
    CLAUDE.md enum rule). Single-use is enforced by the partial unique index, not
    by a read-then-write in Python (mirrors the promo-redemption + USDT tx-claim
    concurrency pattern).
    """
    __tablename__ = "referral_discount_reservations"
    __table_args__ = (
        # At most one live/spent referral discount per user. Partial so that
        # ``released`` (abandoned/expired) rows never block a fresh reservation.
        Index(
            "uq_referral_discount_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'consumed')"),
        ),
        CheckConstraint(
            "status IN ('active', 'consumed', 'released')",
            name="ck_referral_discount_reservations_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referrer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    original_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    discount_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    final_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Gift(Base):
    __tablename__ = "gifts"
    __table_args__ = (
        UniqueConstraint("payment_id", name="uq_gifts_payment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    receiver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    segment: Mapped[str] = mapped_column(String(32), default="all")  # all|active|expired|trial
    status: Mapped[str] = mapped_column(
        SAEnum("draft", "sending", "sent", "failed", name="broadcast_status"),
        default="draft",
    )
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(SAEnum("user", "assistant", "system", name="msg_role"))
    content: Mapped[str] = mapped_column(Text)
    # GK-378: routing/delivery state, free-form string (no enum to dodge the
    # Alembic CREATE TYPE gotcha and keep values cheap to extend). For `user`
    # messages it is the curator-routing outcome: routed | failed | skipped
    # (no curator chat configured) | NULL (legacy / not attempted). For
    # `assistant` replies it is the user-delivery outcome: delivered | failed.
    delivery_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="admin")  # admin|owner|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # GK-405: account "epoch". Signed into every admin JWT (the `ver` claim) and
    # compared in current_admin; bump it to invalidate all previously issued tokens
    # (password/TOTP change, recovery-code login, disable, "revoke all sessions").
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    totp_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    totp_last_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    totp_recovery_hashes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntegrationWebhook(Base):
    __tablename__ = "integration_webhooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)  # amocrm|make|zapier
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    events: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookLog(Base):
    __tablename__ = "webhook_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    webhook_id: Mapped[int | None] = mapped_column(ForeignKey("integration_webhooks.id"), nullable=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ArchiveModule(Base):
    """A grouping of archive videos (e.g. "Тренинг в Нью-Йорке", "Продажи").

    GK-092 (supersedes GK-091's "manual-only" stance): modules are **seeded from
    Vimeo Showcases** (the API calls them *albums*), then editable. A module with
    a `vimeo_album_id` is sync-managed; a module without one is admin-created and
    is **never** touched by the sync. Membership is many-to-many via
    `ArchiveVideoModule` — a video may live under several showcases at once, mirroring
    Vimeo. `ArchiveVideo.module_id` survives only as a derived "primary module"
    convenience for the admin list; grouping is now M2M-driven.

    Admin overrides survive re-sync via a last-synced snapshot (`vimeo_synced`):
    on each sync we only overwrite a field that still equals its last-synced value
    (i.e. the admin hasn't diverged). `is_active` (hide) and `sort_order` are seeded
    once on create and never re-written by sync, so hide/reorder always persist.
    """
    __tablename__ = "archive_modules"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Vimeo showcase/album id this module mirrors. NULL ⇒ admin-created module
    # that sync must never modify or delete. Unique so re-sync upserts in place.
    vimeo_album_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    # Last Vimeo-synced values (JSON snapshot, e.g. {"title": ..., "description": ...}).
    # Used for the 3-way merge that preserves admin renames across re-sync.
    vimeo_synced: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    videos: Mapped[list["ArchiveVideo"]] = relationship(back_populates="module")


class ArchiveVideo(Base):
    """Metadata mirror of a Vimeo-hosted archive video.

    GK-091: the file itself stays in Vimeo; we store only metadata synced via
    the read-only Vimeo API and gate access through our own subscriptions.
    Upsert key is `vimeo_id`; sync never deletes rows (a vanished Vimeo id is
    flagged `visibility='hidden'` so it leaves the portal listing but survives).
    """
    __tablename__ = "archive_videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    vimeo_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # GK-092: derived "primary module" convenience (lowest-sorted active module the
    # video belongs to). Real grouping is the M2M `ArchiveVideoModule`; this is kept
    # for the admin list and back-compat, recomputed when memberships change.
    module_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_modules.id"), nullable=True, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # visible | hidden | draft — controls portal listing; independent of Vimeo.
    visibility: Mapped[str] = mapped_column(String(16), default="visible", index=True)
    # Snapshot of Vimeo's privacy.view value; lets admin warn if a video is
    # unintentionally public (e.g. 'anybody' instead of 'disable').
    vimeo_privacy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    player_embed_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    module: Mapped["ArchiveModule | None"] = relationship(back_populates="videos")


class ArchiveVideoModule(Base):
    """Many-to-many membership: which modules a video belongs to (GK-092).

    Mirrors Vimeo, where one video can live in several showcases at once. Two
    kinds of rows coexist and the sync only ever touches the first:

    - ``source='vimeo'`` — created/reconciled by the showcase sync. When a video
      leaves a Vimeo showcase the row is deleted; when an admin removes such a
      membership in our portal we keep the row but set ``removed_by_admin=True``
      (a tombstone) so the removal survives the next sync.
    - ``source='admin'`` — created by an admin manually adding a video to a
      module. Sync never adds or deletes these.

    ``sort_order`` seeds the per-module video order from Vimeo's showcase order on
    create and is then admin-overridable (sync never rewrites an existing row).
    A membership is *effective* (shown in the portal) when ``removed_by_admin`` is
    False.
    """
    __tablename__ = "archive_video_modules"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("archive_videos.id", ondelete="CASCADE"), index=True
    )
    module_id: Mapped[int] = mapped_column(
        ForeignKey("archive_modules.id", ondelete="CASCADE"), index=True
    )
    # 'vimeo' (sync-owned) | 'admin' (manually added). Plain VARCHAR per the
    # CLAUDE.md enum rule — canonical set validated in the service layer.
    source: Mapped[str] = mapped_column(String(16), default="vimeo")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Admin removed a Vimeo-sourced membership in our portal; the row stays as a
    # tombstone so the next sync does not re-add it. Ignored for source='admin'.
    removed_by_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("video_id", "module_id", name="uq_archive_video_modules_pair"),
    )


class PortalMagicLink(Base):
    """One-time, short-lived token that logs a subscriber into the web portal.

    GK-091: issued by the bot after a `has_portal_access` check, redeemed by the
    portal `/auth/magic` route. Only the SHA-256 hash is stored — even a DB read
    cannot reconstruct a usable link. `used_at` is set atomically on first
    redemption so a second click fails.
    """
    __tablename__ = "portal_magic_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortalSession(Base):
    """Server-side portal session (no JWT — instant revoke + audit history).

    GK-091: the browser cookie `membership_portal_session` carries only the opaque raw
    token; the SHA-256 hash is the lookup key. Revocation is a single UPDATE to
    `revoked_at`; the next request 401s. Access is additionally re-checked
    against `has_portal_access` on every protected request.
    """
    __tablename__ = "portal_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_first_seen: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_last_seen: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    """Immutable record of administrator actions for security investigations.

    We never UPDATE or DELETE rows from this table; the admin UI shows them
    in reverse-chronological order. Stored per-action JSON `details` lets us
    capture before/after diffs for price changes, manual approvals, etc.
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id"), nullable=True, index=True
    )
    actor_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class BackupVerification(Base):
    """GK-437: proof that the newest backup was restorable, and when.

    A backup that cannot be decrypted looks exactly like a backup, which is
    how the corrupt age key on 2026-08-09 went unnoticed. "The backup job
    succeeded" is not the claim that matters; "the newest backup was
    restorable as of X" is. One row per canary run, written by
    `deploy/backup/verify.sh` over psql — the only reader of the private key
    before an emergency.

    Rows are written on failure too, and deliberately so: a verification that
    ran and failed must be visible, not absent. Absence means the canary
    itself died, which the bot's staleness job treats as a failure of its own.
    """
    __tablename__ = "backup_verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    # Basename only — the path is an implementation detail of the container.
    dump_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tables_checked: Mapped[int] = mapped_column(Integer, default=0)
    # Tables whose counts moved on since the dump. Expected and non-fatal;
    # recorded so a sudden zero (or a sudden everything) is visible.
    mismatches: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
