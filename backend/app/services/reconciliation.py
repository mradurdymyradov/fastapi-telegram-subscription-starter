from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import stripe
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import (
    Payment,
    PaymentProviderEvent,
    ReconciliationItem,
    ReconciliationRun,
    Subscription,
    utcnow,
)
from app.payments.lava_provider import LavaSalesGateway, RemoteSale
from app.services.subscription import has_subscription_access

logger = logging.getLogger(__name__)
settings = get_settings()

RECONCILIATION_PROVIDERS = ("stripe", "lava", "usdt")

#: Identity of a *condition* — the thing a curator resolves — as opposed to the
#: identity of a row, which is per-run and therefore useless for that (GK-430).
ConditionKey = tuple[str, str, str, str | None]

_PAID_INVOICE_STATUSES = {"paid"}
_PROVIDER_ACCESS_STATUSES = {"active", "trialing"}
_PROVIDER_TERMINAL_STATUSES = {"canceled", "cancelled", "incomplete_expired", "unpaid"}

#: How far back the Lava remote compare looks (GK-415).
#:
#: Bounded on purpose, and the bound is the GK-426 lesson rather than a
#: performance choice: a remote sale that can never be matched — a hand-fulfilled
#: one, or anything from before the account was ours — would otherwise be
#: re-reported every night for the rest of the project's life. A rolling window
#: lets a genuinely unmatchable sale age out after somebody has had a month to
#: act on it, which is the difference between a report that can reach zero and
#: one that only goes up.
LAVA_REMOTE_LOOKBACK_DAYS = 30
#: How far a remote sale timestamp may sit from a local payment before the two
#: are not the same event. Only used when `clientUtm` is missing.
_LAVA_FALLBACK_MATCH_WINDOW = timedelta(hours=6)


@dataclass(frozen=True)
class ReconciliationIssue:
    provider: str
    issue_type: str
    entity_type: str
    title: str
    description: str
    severity: str = "warning"
    entity_id: str | int | None = None
    external_id: str | None = None
    expected_state: dict[str, Any] = field(default_factory=dict)
    observed_state: dict[str, Any] = field(default_factory=dict)


class StripeGateway(Protocol):
    async def retrieve_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        ...

    async def retrieve_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        ...


class LavaGateway(Protocol):
    async def list_recent_sales(self, *, since: datetime) -> list[RemoteSale]:
        ...


class StripeBillingGateway:
    async def retrieve_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        if not settings.stripe_secret_key:
            return None
        stripe.api_key = settings.stripe_secret_key
        invoice = await asyncio.to_thread(stripe.Invoice.retrieve, invoice_id)
        return _stripe_object_to_dict(invoice)

    async def retrieve_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        if not settings.stripe_secret_key:
            return None
        stripe.api_key = settings.stripe_secret_key
        subscription = await asyncio.to_thread(stripe.Subscription.retrieve, subscription_id)
        return _stripe_object_to_dict(subscription)


async def run_reconciliation(
    session: AsyncSession,
    *,
    providers: list[str] | tuple[str, ...] | None = None,
    triggered_by: str = "scheduler",
    now: datetime | None = None,
    collectors: dict[str, Any] | None = None,
) -> ReconciliationRun:
    checked_at = _coerce_utc(now or utcnow())
    scope = _normalize_providers(providers)
    run = ReconciliationRun(
        status="running",
        triggered_by=triggered_by,
        provider_scope=",".join(scope) if scope != list(RECONCILIATION_PROVIDERS) else "all",
        started_at=checked_at,
    )
    session.add(run)
    await session.flush()

    issues: list[ReconciliationIssue] = []
    errors: dict[str, str] = {}
    collector_map = collectors or {
        "stripe": collect_stripe_discrepancies,
        "lava": collect_lava_discrepancies,
        "usdt": collect_usdt_discrepancies,
    }

    for provider in scope:
        collector = collector_map[provider]
        try:
            issues.extend(await collector(session, checked_at))
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconciliation provider collection failed: %s", provider)
            errors[provider] = type(exc).__name__
            issues.append(
                ReconciliationIssue(
                    provider=provider,
                    severity="critical",
                    issue_type="provider_collection_failed",
                    entity_type="provider",
                    entity_id=provider,
                    title=f"{provider} reconciliation failed",
                    description=(
                        "Provider comparison failed before it could finish. "
                        "Check backend logs and rerun reconciliation after fixing the cause."
                    ),
                    expected_state={"collector": "completed"},
                    observed_state={"error": type(exc).__name__},
                )
            )

    history = await load_condition_history(session)
    recorded: list[ReconciliationIssue] = []
    seen: set[ConditionKey] = set()
    counts = {"new": 0, "recurring": 0, "carried_resolved": 0, "duplicates_dropped": 0}
    open_count = 0

    for issue in issues:
        key = issue_condition_key(issue)
        if key in seen:
            # The same condition reached twice in one run (two collectors, or one
            # entity failing two ways that normalize to the same key) is one row.
            counts["duplicates_dropped"] += 1
            continue
        seen.add(key)

        item = _issue_to_model(run.id, issue)
        prior = history.get(key)
        if prior is None:
            counts["new"] += 1
            item.first_seen_at = checked_at
        else:
            item.first_seen_at = getattr(prior, "first_seen_at", None) or prior.created_at
            if prior.status == "resolved":
                counts["carried_resolved"] += 1
                item.status = "resolved"
                item.resolve_note = prior.resolve_note
                item.resolved_by_admin_id = prior.resolved_by_admin_id
                item.resolved_at = prior.resolved_at
            else:
                counts["recurring"] += 1

        if item.status == "open":
            open_count += 1
        session.add(item)
        recorded.append(issue)

    run.items_count = len(recorded)
    run.open_items_count = open_count
    run.summary = _summary_for_issues(recorded, errors=errors, counts=counts)
    run.status = "completed_with_errors" if errors else "completed"
    run.error = "; ".join(f"{k}: {v}" for k, v in errors.items()) or None
    run.finished_at = utcnow()
    await session.flush()
    return run


async def load_condition_history(session: AsyncSession) -> dict[ConditionKey, ReconciliationItem]:
    """The newest row for every condition ever detected, keyed by condition.

    GK-430: a resolution has to survive the next run, and the next sixty. The
    nightly job re-detects the same conditions every morning; keying the lookup
    on the **condition** — provider, issue type, entity — rather than on the run
    is what makes "checked this one, it is explainable" stick, instead of being
    re-manufactured as a fresh open row while the curator sleeps. Before this,
    312 items had ever been created and 6 had ever been resolved, because
    resolving one only ever silenced that single row in that single run.

    One row per condition comes back, so re-opening an item in the panel is
    respected too: the newest row wins, and if it is open the condition is open.
    """
    newest = (
        select(func.max(ReconciliationItem.id).label("id"))
        .group_by(
            ReconciliationItem.provider,
            ReconciliationItem.issue_type,
            ReconciliationItem.entity_type,
            ReconciliationItem.entity_id,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ReconciliationItem).join(newest, ReconciliationItem.id == newest.c.id)
        )
    ).scalars().all()
    return {item_condition_key(row): row for row in rows}


def issue_condition_key(issue: ReconciliationIssue) -> ConditionKey:
    return _condition_key(issue.provider, issue.issue_type, issue.entity_type, issue.entity_id)


def item_condition_key(item: ReconciliationItem) -> ConditionKey:
    return _condition_key(item.provider, item.issue_type, item.entity_type, item.entity_id)


def _condition_key(
    provider: str,
    issue_type: str,
    entity_type: str,
    entity_id: str | int | None,
) -> ConditionKey:
    # Normalized exactly as `_issue_to_model` stores it, so a key built from a
    # fresh issue matches the key built from the row it was persisted as.
    return (provider, issue_type, entity_type, _entity_key(entity_id))


async def collect_stripe_discrepancies(
    session: AsyncSession,
    now: datetime,
    *,
    gateway: StripeGateway | None = None,
) -> list[ReconciliationIssue]:
    payments = (
        await session.execute(
            select(Payment)
            .options(selectinload(Payment.refunds))
            .where(Payment.provider == "stripe")
            .order_by(Payment.created_at.desc())
            .limit(1000)
        )
    ).scalars().all()
    subscriptions = (
        await session.execute(
            select(Subscription)
            .where(or_(Subscription.provider == "stripe", Subscription.source == "stripe"))
            .order_by(Subscription.expires_at.desc())
            .limit(1000)
        )
    ).scalars().all()
    events = (
        await session.execute(
            select(PaymentProviderEvent)
            .where(PaymentProviderEvent.provider == "stripe")
            .order_by(PaymentProviderEvent.processed_at.desc())
            .limit(2000)
        )
    ).scalars().all()

    issues = scan_stripe_local(payments, subscriptions, events, now)
    if settings.stripe_secret_key:
        issues.extend(
            await compare_stripe_remote(
                payments,
                subscriptions,
                gateway=gateway or StripeBillingGateway(),
            )
        )
    return issues


async def collect_lava_discrepancies(
    session: AsyncSession,
    now: datetime,
    *,
    gateway: LavaGateway | None = None,
) -> list[ReconciliationIssue]:
    payments = (
        await session.execute(
            select(Payment)
            .options(selectinload(Payment.refunds))
            .where(Payment.provider == "lava")
            .order_by(Payment.created_at.desc())
            .limit(1000)
        )
    ).scalars().all()
    subscriptions = (
        await session.execute(
            select(Subscription)
            .where(or_(Subscription.provider == "lava", Subscription.source == "lava"))
            .order_by(Subscription.expires_at.desc())
            .limit(1000)
        )
    ).scalars().all()
    events = (
        await session.execute(
            select(PaymentProviderEvent)
            .where(PaymentProviderEvent.provider == "lava")
            .order_by(PaymentProviderEvent.processed_at.desc())
            .limit(2000)
        )
    ).scalars().all()
    issues = scan_lava_local(payments, subscriptions, events, now)
    # Mirrors the Stripe shape: local scan always, remote compare only when a key
    # exists. Without one the reconciliation run is unchanged from before GK-415.
    if settings.lava_api_key:
        issues.extend(
            await compare_lava_remote(
                payments,
                subscriptions,
                gateway=gateway or LavaSalesGateway(),
                now=now,
            )
        )
    return issues


async def collect_usdt_discrepancies(
    session: AsyncSession,
    now: datetime,
) -> list[ReconciliationIssue]:
    payments = (
        await session.execute(
            select(Payment)
            .options(selectinload(Payment.refunds))
            .where(Payment.provider == "usdt")
            .order_by(Payment.created_at.desc())
            .limit(2000)
        )
    ).scalars().all()
    return scan_usdt_local(payments, now)


def refund_consistency_issues(
    provider: str,
    payments: list[Payment],
) -> list[ReconciliationIssue]:
    """Flag local drift between recorded refunds and payment state (GK-200).

    Pure-local data-integrity check so a manual DB poke or a half-applied refund
    cannot silently leave a payment in a contradictory state. Provider-agnostic;
    called from each provider's local scan with that provider's payments.
    """
    issues: list[ReconciliationIssue] = []
    for payment in payments:
        total = _decimal(getattr(payment, "amount", 0))
        refunded = _decimal(getattr(payment, "refunded_amount", 0))
        status = getattr(payment, "status", None)
        refund_rows = list(getattr(payment, "__dict__", {}).get("refunds") or [])

        confirmed_sum = sum(
            (
                _decimal(refund.amount)
                for refund in refund_rows
                if refund.status == "provider_confirmed"
                and refund.accounting_applied_at is not None
            ),
            Decimal("0"),
        )
        if refund_rows and confirmed_sum != refunded:
            issues.append(
                ReconciliationIssue(
                    provider=provider,
                    severity="critical",
                    issue_type=f"{provider}_confirmed_refund_total_mismatch",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=getattr(payment, "external_id", None),
                    title="Confirmed refund rows do not match recognized refunded cash",
                    description=(
                        "Payment.refunded_amount must equal the sum of accounting-applied "
                        "provider-confirmed refund rows."
                    ),
                    expected_state={"confirmed_refund_total": str(refunded)},
                    observed_state={
                        "confirmed_refund_total": str(confirmed_sum),
                        "payment": _payment_state(payment),
                    },
                )
            )

        for refund in refund_rows:
            refund_status = str(getattr(refund, "status", "") or "")
            if refund_status in {"requested", "pending", "manual_action_required"}:
                label = refund_status.replace("_", " ")
                issues.append(
                    ReconciliationIssue(
                        provider=provider,
                        severity="warning",
                        issue_type=f"{provider}_refund_{refund_status}",
                        entity_type="refund",
                        entity_id=refund.id,
                        external_id=getattr(refund, "provider_refund_id", None),
                        title=f"Refund is {label}",
                        description=(
                            "This refund has not been provider-confirmed, so no cash, "
                            "access, or partner-ledger effects have been applied."
                        ),
                        expected_state={"next": "provider_confirmed or failed"},
                        observed_state={
                            "status": refund_status,
                            "provider_status": getattr(refund, "provider_status", None),
                            "amount": str(getattr(refund, "amount", 0)),
                            "failure_reason": getattr(refund, "failure_reason", None),
                        },
                    )
                )
            elif refund_status == "failed":
                issues.append(
                    ReconciliationIssue(
                        provider=provider,
                        severity="warning",
                        issue_type=f"{provider}_refund_failed",
                        entity_type="refund",
                        entity_id=refund.id,
                        external_id=getattr(refund, "provider_refund_id", None),
                        title="Refund failed",
                        description="The refund failed and did not change recognized cash or access.",
                        expected_state={"follow_up": "retry or resolve with an audited note"},
                        observed_state={
                            "provider_status": getattr(refund, "provider_status", None),
                            "failure_reason": getattr(refund, "failure_reason", None),
                        },
                    )
                )
            elif (
                refund_status == "provider_confirmed"
                and getattr(refund, "accounting_applied_at", None) is None
            ):
                issues.append(
                    ReconciliationIssue(
                        provider=provider,
                        severity="critical",
                        issue_type=f"{provider}_confirmed_refund_not_applied",
                        entity_type="refund",
                        entity_id=refund.id,
                        external_id=getattr(refund, "provider_refund_id", None),
                        title="Provider-confirmed refund has no accounting application marker",
                        description=(
                            "Provider confirmation and local accounting must commit atomically; "
                            "investigate before changing totals manually."
                        ),
                        expected_state={"accounting_applied_at": "present"},
                        observed_state={"accounting_applied_at": None},
                    )
                )

        if refunded <= 0:
            if status == "refunded":
                issues.append(
                    ReconciliationIssue(
                        provider=provider,
                        severity="warning",
                        issue_type=f"{provider}_refunded_without_refund_record",
                        entity_type="payment",
                        entity_id=payment.id,
                        external_id=getattr(payment, "external_id", None),
                        title="Payment marked refunded but no refund amount is recorded",
                        description="Status is 'refunded' yet refunded_amount is zero; record or reverse the refund.",
                        expected_state={"refunded_amount": "> 0"},
                        observed_state=_payment_state(payment),
                    )
                )
            continue

        if refunded > total:
            issues.append(
                ReconciliationIssue(
                    provider=provider,
                    severity="critical",
                    issue_type=f"{provider}_refund_exceeds_payment",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=getattr(payment, "external_id", None),
                    title="Refunded amount exceeds the payment amount",
                    description="Total recorded refunds are larger than the original charge.",
                    expected_state={"refunded_amount": "<= amount"},
                    observed_state=_payment_state(payment),
                )
            )
            continue

        fully_refunded = refunded >= total
        if fully_refunded and status != "refunded":
            issues.append(
                ReconciliationIssue(
                    provider=provider,
                    severity="critical",
                    issue_type=f"{provider}_full_refund_status_mismatch",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=getattr(payment, "external_id", None),
                    title="Payment is fully refunded but status is not 'refunded'",
                    description="The full amount was refunded, so the payment status should be 'refunded'.",
                    expected_state={"status": "refunded"},
                    observed_state=_payment_state(payment),
                )
            )
        elif not fully_refunded and status == "refunded":
            issues.append(
                ReconciliationIssue(
                    provider=provider,
                    severity="warning",
                    issue_type=f"{provider}_partial_refund_status_mismatch",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=getattr(payment, "external_id", None),
                    title="Payment is only partially refunded but status is 'refunded'",
                    description="Only part of the charge was refunded; status should stay 'succeeded'.",
                    expected_state={"status": "succeeded"},
                    observed_state=_payment_state(payment),
                )
            )
    return issues


def current_provider_subscription_rows(
    subscriptions: list[Subscription],
) -> list[Subscription]:
    """GK-426: keep at most one row per ``provider_subscription_id`` — the live one.

    Renewals after a lapse leave superseded rows behind that still carry the
    provider id and a frozen ``provider_status``; comparing those against live
    provider state manufactures a permanent critical per stale row per run.
    ``release_superseded_provider_link`` clears the link at renewal time, and a
    migration backfilled the rows that predate it. This is the belt-and-braces
    filter: even if a stale row reappears — a hand-edited row, an older release
    writing history, a provider replaying an old contract — reconciliation still
    only judges the row that represents the subscription *now*.

    "Now" is the same rule the webhook lookups use (``expires_at`` desc, newest
    id as tie-break), so the row reconciliation judges is the row a provider
    event would update. Rows with no provider id pass through untouched: their
    own checks (``…_active_subscription_missing_provider_id``) still need them —
    but only while they hold access. GK-469: this function releases the link and
    the row keeps ``status="active"``, so that check had to learn the same
    liveness rule or it simply flagged what this one had just cleared.
    """
    winners: dict[str, Subscription] = {}
    for sub in subscriptions:
        key = getattr(sub, "provider_subscription_id", None)
        if not key:
            continue
        incumbent = winners.get(key)
        if incumbent is None or _liveness_key(sub) > _liveness_key(incumbent):
            winners[key] = sub

    kept = {id(row) for row in winners.values()}
    return [
        sub
        for sub in subscriptions
        if not getattr(sub, "provider_subscription_id", None) or id(sub) in kept
    ]


def _liveness_key(sub: Subscription) -> tuple[datetime, int]:
    expires_at = _coerce_optional_utc(getattr(sub, "expires_at", None))
    return (expires_at or datetime.min.replace(tzinfo=UTC), int(getattr(sub, "id", 0) or 0))


def _is_comp_row(sub: Subscription) -> bool:
    """GK-483's team flag, read defensively — see the long note below.

    A comp row is team access, not a billing relationship: nobody paid, no
    provider ever issued a contract for it, and so "this active row is missing
    its provider subscription id" is not a finding about it. It is the row's
    definition.

    **Why this is a `getattr` and not `from app.services.subscription import
    is_comp_access`.** The two halves of this defect live on different branches.
    GK-483 adds `Subscription.is_comp` and the `is_comp_access` helper; GK-469
    (this file) adds the liveness guard on the missing-link rules. Neither
    branch is wrong on its own, and neither could have caught what happens when
    they meet: `is_comp_access` makes `has_subscription_access` return True for
    a comp row *whatever its dates say*, and the guard below reads exactly that
    predicate. So flagging the one row GK-484 names — `sub#2`, Grant, `active`,
    provider link nulled by `0024` — flips it from "entitles nobody, stays
    quiet" to "live, must have a provider id", and reconciliation raises a
    critical against it every night forever. That is precisely the false class
    GK-469 exists to remove, re-manufactured by the fix for a different task.

    Importing the helper would be the better expression of the rule and is what
    this should become. It cannot be that yet: on this branch alone the symbol
    does not exist, so the import would fail at module load and take the whole
    backend down. `getattr` is inert here (no column, always False, no
    behaviour change) and correct the moment GK-483 is merged ahead of this
    branch, which is the recommended order.

    **Collapse this into `is_comp_access` once both are on `main`.** GK-483's
    docstring calls that helper "the single place the comp rule is expressed",
    and while this function exists that is one place too few. Recorded as debt
    in the GK-469 task block rather than left for someone to find.
    """
    return bool(getattr(sub, "is_comp", False))


def scan_stripe_local(
    payments: list[Payment],
    subscriptions: list[Subscription],
    events: list[PaymentProviderEvent],
    now: datetime,
) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = refund_consistency_issues("stripe", payments)
    payments_by_id = {p.id: p for p in payments}
    subscriptions = current_provider_subscription_rows(subscriptions)

    for payment in payments:
        if payment.status == "succeeded" and not payment.stripe_invoice_id:
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_succeeded_missing_invoice",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.stripe_checkout_session_id or payment.external_id,
                    title="Stripe payment succeeded without invoice evidence",
                    description=(
                        "Stripe entitlement should be backed by invoice.paid, "
                        "not only by a checkout session."
                    ),
                    expected_state={"stripe_invoice_id": "present"},
                    observed_state=_payment_state(payment),
                )
            )
        if payment.stripe_invoice_id and payment.status == "succeeded" and not payment.provider_event_id:
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    issue_type="stripe_invoice_missing_event_id",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.stripe_invoice_id,
                    title="Stripe invoice payment lacks provider event id",
                    description="A paid invoice exists locally but is not tied to the processed Stripe event.",
                    expected_state={"provider_event_id": "invoice.paid event id"},
                    observed_state=_payment_state(payment),
                )
            )

    for event in events:
        event_type = event.event_type or ""
        if event_type == "invoice.paid":
            payment = payments_by_id.get(event.payment_id) if event.payment_id else None
            if payment is None:
                issues.append(_unlinked_event_issue("stripe", event, "invoice.paid"))
            elif payment.status != "succeeded":
                issues.append(
                    ReconciliationIssue(
                        provider="stripe",
                        severity="critical",
                        issue_type="stripe_paid_invoice_not_succeeded",
                        entity_type="payment",
                        entity_id=payment.id,
                        external_id=event.event_id,
                        title="Stripe invoice.paid did not produce succeeded payment",
                        description="Webhook history says the invoice was paid, but the local payment is not succeeded.",
                        expected_state={"payment.status": "succeeded"},
                        observed_state={
                            "payment": _payment_state(payment),
                            "event": _event_state(event),
                        },
                    )
                )
        elif event_type == "invoice.payment_failed" and event.payment_id:
            payment = payments_by_id.get(event.payment_id)
            if payment is not None and payment.status == "succeeded":
                issues.append(
                    ReconciliationIssue(
                        provider="stripe",
                        severity="warning",
                        issue_type="stripe_failed_invoice_payment_succeeded",
                        entity_type="payment",
                        entity_id=payment.id,
                        external_id=event.event_id,
                        title="Stripe failed invoice is linked to succeeded payment",
                        description="Review whether this is a later retry, a stale local row, or a refund/cancel path.",
                        expected_state={"failed invoice payment.status": "failed or pending"},
                        observed_state={
                            "payment": _payment_state(payment),
                            "event": _event_state(event),
                        },
                    )
                )

    for sub in subscriptions:
        provider_status = (sub.provider_status or "").lower()
        # GK-469: judge only a row that grants access *now*. GK-426 releases the
        # provider link on a superseded row, and that row keeps `status="active"`
        # — the flip to "expired" happens only when a Telegram revoke succeeds,
        # which for a member who renewed may never have run. Without this guard
        # the fix that removed one permanent false critical manufactured another
        # on the very same rows, which is what the 19.08 audit measured. A row
        # that entitles nobody has no provider state worth comparing; a live one
        # still does, and still reports.
        #
        # GK-483 interaction: a comp row is live by construction — the flag ends
        # the expiry clock, so `has_subscription_access` is True for it whatever
        # its dates say. It also has no provider contract and never had one, so
        # without `_is_comp_row` the guard above would report every flagged team
        # member as a critical, nightly. See `_is_comp_row`.
        if (
            sub.status == "active"
            and not sub.provider_subscription_id
            and has_subscription_access(sub, now)
            and not _is_comp_row(sub)
        ):
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_active_subscription_missing_provider_id",
                    entity_type="subscription",
                    entity_id=sub.id,
                    title="Active Stripe subscription lacks provider subscription id",
                    description="Reconciliation cannot compare Stripe state without the subscription id.",
                    expected_state={"provider_subscription_id": "present"},
                    observed_state=_subscription_state(sub),
                )
            )
        if provider_status in _PROVIDER_ACCESS_STATUSES and not has_subscription_access(sub, now):
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_provider_active_without_local_access",
                    entity_type="subscription",
                    entity_id=sub.id,
                    external_id=sub.provider_subscription_id,
                    title="Stripe provider state says active but local access is closed",
                    description="A subscriber may be paying in Stripe while local Telegram/portal access is unavailable.",
                    expected_state={"local_access": True},
                    observed_state=_subscription_state(sub),
                )
            )
        if (
            provider_status in _PROVIDER_TERMINAL_STATUSES
            and has_subscription_access(sub, now)
            and not sub.cancel_at_period_end
        ):
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_provider_terminal_with_local_access",
                    entity_type="subscription",
                    entity_id=sub.id,
                    external_id=sub.provider_subscription_id,
                    title="Stripe provider state is terminal but local access remains open",
                    description="Review cancellation/grace handling before the user keeps access incorrectly.",
                    expected_state={"local_access": False},
                    observed_state=_subscription_state(sub),
                )
            )
        if provider_status == "past_due" and sub.grace_ends_at is None:
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    issue_type="stripe_past_due_missing_grace",
                    entity_type="subscription",
                    entity_id=sub.id,
                    external_id=sub.provider_subscription_id,
                    title="Stripe past_due subscription has no grace window",
                    description="Failed renewals should enter provider grace before access is revoked.",
                    expected_state={"grace_ends_at": "present"},
                    observed_state=_subscription_state(sub),
                )
            )

    return issues


async def compare_stripe_remote(
    payments: list[Payment],
    subscriptions: list[Subscription],
    *,
    gateway: StripeGateway,
) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = []
    for payment in payments:
        if not payment.stripe_invoice_id:
            continue
        try:
            invoice = await gateway.retrieve_invoice(payment.stripe_invoice_id)
        except Exception as exc:  # noqa: BLE001
            issues.append(_remote_fetch_issue("stripe", "invoice", payment.stripe_invoice_id, exc))
            continue
        if not invoice:
            continue
        invoice_status = str(invoice.get("status") or "").lower()
        if invoice_status in _PAID_INVOICE_STATUSES and payment.status != "succeeded":
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_remote_paid_invoice_not_succeeded",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.stripe_invoice_id,
                    title="Stripe invoice is paid remotely but local payment is not succeeded",
                    description="Remote invoice state should drive entitlement fulfillment.",
                    expected_state={"payment.status": "succeeded"},
                    observed_state={"payment": _payment_state(payment), "remote_invoice": invoice},
                )
            )
        if payment.status == "succeeded" and invoice_status and invoice_status not in _PAID_INVOICE_STATUSES:
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_local_succeeded_remote_invoice_not_paid",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.stripe_invoice_id,
                    title="Local Stripe payment succeeded but remote invoice is not paid",
                    description="Review refunds, voided invoices, and stale local state.",
                    expected_state={"remote_invoice.status": "paid"},
                    observed_state={"payment": _payment_state(payment), "remote_invoice": invoice},
                )
            )

    for sub in current_provider_subscription_rows(subscriptions):
        if not sub.provider_subscription_id:
            continue
        try:
            remote_sub = await gateway.retrieve_subscription(sub.provider_subscription_id)
        except Exception as exc:  # noqa: BLE001
            issues.append(
                _remote_fetch_issue("stripe", "subscription", sub.provider_subscription_id, exc)
            )
            continue
        if not remote_sub:
            continue
        remote_status = str(remote_sub.get("status") or "").lower()
        if remote_status in _PROVIDER_ACCESS_STATUSES and not has_subscription_access(sub):
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_remote_active_subscription_without_local_access",
                    entity_type="subscription",
                    entity_id=sub.id,
                    external_id=sub.provider_subscription_id,
                    title="Stripe subscription is active remotely but local access is unavailable",
                    description="Remote subscription state and local access predicate disagree.",
                    expected_state={"local_access": True},
                    observed_state={"subscription": _subscription_state(sub), "remote": remote_sub},
                )
            )
        # GK-426: `cancel_at_period_end` is the normal shape of a cancellation —
        # terminal at the provider, legitimately still open locally until the
        # paid period runs out. The local rule above already excludes it; without
        # the same guard here every ordinary cancellation is a nightly critical.
        if (
            remote_status in _PROVIDER_TERMINAL_STATUSES
            and has_subscription_access(sub)
            and not sub.cancel_at_period_end
        ):
            issues.append(
                ReconciliationIssue(
                    provider="stripe",
                    severity="critical",
                    issue_type="stripe_remote_terminal_subscription_with_local_access",
                    entity_type="subscription",
                    entity_id=sub.id,
                    external_id=sub.provider_subscription_id,
                    title="Stripe subscription is terminal remotely but local access is still open",
                    description="Review whether the user is inside a paid period/grace or should be revoked.",
                    expected_state={"local_access": False},
                    observed_state={"subscription": _subscription_state(sub), "remote": remote_sub},
                )
            )
    return issues


async def compare_lava_remote(
    payments: list[Payment],
    subscriptions: list[Subscription],
    *,
    gateway: LavaGateway,
    now: datetime,
) -> list[ReconciliationIssue]:
    """Ask Lava what it sold, and account for every completed sale locally.

    The opposite direction from `compare_stripe_remote`, which walks local rows
    and looks each one up. The failure this exists for — Grant's ask, and a real
    16.07 event — is a sale that is *only* remote: a buyer paid, nothing reached
    us, and there is therefore no local row to start from and no webhook to
    alert on. Walking our own payments could never see it.

    Alert only. Nothing here fulfils anything, deliberately: an auto-grant
    driven by a mis-parsed remote feed is worse than a late manual grant, and
    the feed's exact shape is the part we are least sure of.
    """
    since = _coerce_utc(now) - timedelta(days=LAVA_REMOTE_LOOKBACK_DAYS)
    try:
        remote_sales = await gateway.list_recent_sales(since=since)
    except Exception as exc:  # noqa: BLE001 — a dead journal must not stop the run
        return [_remote_fetch_issue("lava", "invoice_journal", "recent_sales", exc)]

    # Both sides are windowed: the caller passes the newest 1000 Lava payments,
    # this looks back 30 days. A sale can only fall outside the payment window if
    # we took more than a thousand Lava payments in a month, at which point the
    # limit in `collect_lava_discrepancies` is the thing to raise.
    by_id = {payment.id: payment for payment in payments}
    fulfilled_user_ids = {
        sub.user_id for sub in subscriptions if sub.user_id is not None
    }

    issues: list[ReconciliationIssue] = []
    for sale in remote_sales:
        if not sale.is_completed:
            continue
        if sale.created_at is not None and _coerce_utc(sale.created_at) < since:
            continue

        payment = _match_remote_sale(sale, payments, by_id)
        if payment is None:
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    severity="critical",
                    issue_type="lava_remote_sale_without_local_payment",
                    entity_type="remote_sale",
                    entity_id=sale.id,
                    external_id=sale.id,
                    title="Lava reports a completed sale we have no payment for",
                    description=(
                        "Money reached Lava and nothing reached us, so nobody was "
                        "granted access. Fulfil by hand from the buyer email and "
                        "amount below, then check why the webhook did not arrive."
                    ),
                    expected_state={"local_payment": "exists and succeeded"},
                    observed_state={"remote_sale": sale.describe()},
                )
            )
            continue

        if payment.status != "succeeded":
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    severity="critical",
                    issue_type="lava_remote_completed_local_not_succeeded",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=sale.id,
                    title="Lava completed the sale but the local payment is not succeeded",
                    description="The buyer has paid; entitlement was never granted.",
                    expected_state={"payment.status": "succeeded"},
                    observed_state={
                        "payment": _payment_state(payment),
                        "remote_sale": sale.describe(),
                    },
                )
            )
            continue

        mismatch = _sale_amount_mismatch(sale, payment)
        if mismatch is not None:
            # Never a silent pass: a sale we matched but cannot agree the price
            # of is not evidence that the right thing was sold (GK-412's
            # fail-closed rule, applied to the read side).
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    severity="critical",
                    issue_type="lava_remote_amount_mismatch",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=sale.id,
                    title="Lava sale amount does not match the local payment",
                    description="Check the plan, the promo discount and the currency before acting.",
                    expected_state=mismatch["expected"],
                    observed_state={
                        "payment": _payment_state(payment),
                        "remote_sale": sale.describe(),
                        **mismatch["observed"],
                    },
                )
            )
            continue

        if payment.user_id not in fulfilled_user_ids:
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    severity="critical",
                    issue_type="lava_remote_sale_never_fulfilled",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=sale.id,
                    title="Lava sale is succeeded locally but produced no subscription",
                    description="The payment was recorded and the member still has no access row.",
                    expected_state={"subscription": "exists for the payer"},
                    observed_state={
                        "payment": _payment_state(payment),
                        "remote_sale": sale.describe(),
                    },
                )
            )
    return issues


def _match_remote_sale(
    sale: RemoteSale,
    payments: list[Payment],
    by_id: dict[int, Payment],
) -> Payment | None:
    """Join a remote sale to a local payment.

    `clientUtm.utm_content = payment_{id}` is the correlation key and is exact —
    we set it ourselves when creating the invoice, and it came back unchanged on
    real money. The email/amount/timestamp fallback exists only for sales made
    outside that path; it is deliberately narrow, because a loose match here
    would silence a genuine "paid but not fulfilled" by attaching it to somebody
    else's payment.
    """
    if sale.payment_id is not None:
        matched = by_id.get(sale.payment_id)
        if matched is not None:
            return matched
        # A UTM naming a payment we do not have is not a reason to go guessing
        # by email — it is the finding.
        return None

    if sale.id:
        for payment in payments:
            if payment.lava_invoice_id == sale.id or payment.external_id == sale.id:
                return payment

    if not sale.buyer_email or sale.amount is None or sale.created_at is None:
        return None

    created = _coerce_utc(sale.created_at)
    for payment in payments:
        note = (payment.note or "").lower()
        if f"buyer_email={sale.buyer_email}" not in note:
            continue
        if _decimal(payment.amount) != sale.amount:
            continue
        if (payment.currency or "").upper() != sale.currency:
            continue
        local_created = _coerce_optional_utc(payment.created_at)
        if local_created is None:
            continue
        if abs(local_created - created) <= _LAVA_FALLBACK_MATCH_WINDOW:
            return payment
    return None


def _sale_amount_mismatch(sale: RemoteSale, payment: Payment) -> dict[str, Any] | None:
    """Amount/currency disagreement, or None when they agree or cannot be read."""
    if sale.amount is None or not sale.currency:
        # Unreadable is not the same as wrong. Reporting it as a mismatch would
        # turn every shape change in Lava's journal into a night of criticals.
        return None
    local_amount = _decimal(payment.amount)
    local_currency = (payment.currency or "").upper()
    if sale.amount == local_amount and sale.currency == local_currency:
        return None
    return {
        "expected": {"amount": str(local_amount), "currency": local_currency},
        "observed": {"remote_amount": str(sale.amount), "remote_currency": sale.currency},
    }


def scan_lava_local(
    payments: list[Payment],
    subscriptions: list[Subscription],
    events: list[PaymentProviderEvent],
    now: datetime,
) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = refund_consistency_issues("lava", payments)
    payments_by_id = {p.id: p for p in payments}
    subscriptions = current_provider_subscription_rows(subscriptions)

    for payment in payments:
        if payment.status == "succeeded" and not payment.lava_invoice_id:
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    severity="critical",
                    issue_type="lava_succeeded_missing_invoice",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.external_id,
                    title="Lava payment succeeded without invoice evidence",
                    description="Lava reconciliation depends on invoice/report or webhook history.",
                    expected_state={"lava_invoice_id": "present"},
                    observed_state=_payment_state(payment),
                )
            )
        if payment.status == "succeeded" and not payment.provider_event_id:
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    issue_type="lava_succeeded_missing_event_id",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.lava_invoice_id or payment.external_id,
                    title="Lava succeeded payment lacks webhook event id",
                    description="Refunds and disputes must be reconciled against provider webhook/report history.",
                    expected_state={"provider_event_id": "present"},
                    observed_state=_payment_state(payment),
                )
            )

    for event in events:
        event_type = event.event_type or ""
        if event_type in {"payment.success", "subscription.recurring.payment.success"}:
            payment = payments_by_id.get(event.payment_id) if event.payment_id else None
            if payment is None:
                issues.append(_unlinked_event_issue("lava", event, event_type))
            elif payment.status != "succeeded":
                issues.append(
                    ReconciliationIssue(
                        provider="lava",
                        severity="critical",
                        issue_type="lava_success_event_not_succeeded",
                        entity_type="payment",
                        entity_id=payment.id,
                        external_id=event.event_id,
                        title="Lava success webhook did not produce succeeded payment",
                        description="Webhook history says Lava payment succeeded, but local payment is not succeeded.",
                        expected_state={"payment.status": "succeeded"},
                        observed_state={
                            "payment": _payment_state(payment),
                            "event": _event_state(event),
                        },
                    )
                )
        elif event_type in {"payment.failed", "subscription.recurring.payment.failed"} and event.payment_id:
            payment = payments_by_id.get(event.payment_id)
            if payment is not None and payment.status == "succeeded":
                issues.append(
                    ReconciliationIssue(
                        provider="lava",
                        issue_type="lava_failed_event_payment_succeeded",
                        entity_type="payment",
                        entity_id=payment.id,
                        external_id=event.event_id,
                        title="Lava failed webhook is linked to succeeded payment",
                        description="Review whether this was later recovered or local state is stale.",
                        expected_state={"failed event payment.status": "failed or pending"},
                        observed_state={
                            "payment": _payment_state(payment),
                            "event": _event_state(event),
                        },
                    )
                )

    for sub in subscriptions:
        provider_status = (sub.provider_status or "").lower()
        # GK-469: the same liveness guard as the Stripe scan, for the same
        # reason — a superseded row released by GK-426 keeps `status="active"`.
        # And the same GK-483 comp exclusion, because a row reaches this scan on
        # `provider == "lava" OR source == "lava"` — flagging a member who once
        # paid through Lava puts their row in front of this rule too.
        if (
            sub.status == "active"
            and not sub.provider_subscription_id
            and has_subscription_access(sub, now)
            and not _is_comp_row(sub)
        ):
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    issue_type="lava_active_subscription_missing_provider_id",
                    entity_type="subscription",
                    entity_id=sub.id,
                    title="Active Lava subscription lacks provider subscription id",
                    description="Lava recurring reconciliation cannot match reports/webhooks without this id.",
                    expected_state={"provider_subscription_id": "present"},
                    observed_state=_subscription_state(sub),
                )
            )
        if provider_status == "past_due" and sub.grace_ends_at is None:
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    issue_type="lava_past_due_missing_grace",
                    entity_type="subscription",
                    entity_id=sub.id,
                    external_id=sub.provider_subscription_id,
                    title="Lava past_due subscription has no grace window",
                    description="Failed Lava renewals should enter provider grace before access is revoked.",
                    expected_state={"grace_ends_at": "present"},
                    observed_state=_subscription_state(sub),
                )
            )
        if (
            provider_status in _PROVIDER_TERMINAL_STATUSES
            and has_subscription_access(sub, now)
            and not sub.cancel_at_period_end
        ):
            issues.append(
                ReconciliationIssue(
                    provider="lava",
                    severity="critical",
                    issue_type="lava_terminal_provider_state_with_local_access",
                    entity_type="subscription",
                    entity_id=sub.id,
                    external_id=sub.provider_subscription_id,
                    title="Lava provider state is terminal but local access remains open",
                    description="Review Lava cancellation handling and local revoke timing.",
                    expected_state={"local_access": False},
                    observed_state=_subscription_state(sub),
                )
            )
    return issues


def scan_usdt_local(payments: list[Payment], now: datetime) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = refund_consistency_issues("usdt", payments)
    duplicate_claims: dict[tuple[str, str], list[Payment]] = {}

    for payment in payments:
        network = (payment.tx_network or "").upper()
        tx_hash = (payment.tx_hash or "").lower().removeprefix("0x")
        if network and tx_hash:
            duplicate_claims.setdefault((network, tx_hash), []).append(payment)

        if payment.status == "succeeded" and (
            not payment.tx_hash or not payment.tx_network or payment.tx_confirmed_at is None
        ):
            issues.append(
                ReconciliationIssue(
                    provider="usdt",
                    severity="critical",
                    issue_type="usdt_succeeded_missing_verification",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.tx_hash,
                    title="USDT payment succeeded without complete tx verification state",
                    description="Succeeded USDT payments must carry network, tx hash, and confirmed timestamp.",
                    expected_state={
                        "tx_hash": "present",
                        "tx_network": "TRC20 or ERC20",
                        "tx_confirmed_at": "present",
                    },
                    observed_state=_payment_state(payment),
                )
            )
        if payment.status == "failed" and payment.tx_confirmed_at is not None:
            issues.append(
                ReconciliationIssue(
                    provider="usdt",
                    severity="critical",
                    issue_type="usdt_confirmed_tx_failed_payment",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.tx_hash,
                    title="USDT payment is failed despite confirmed tx state",
                    description="A confirmed transaction should not remain failed without an explicit admin note.",
                    expected_state={"payment.status": "succeeded or awaiting_review with note"},
                    observed_state=_payment_state(payment),
                )
            )
        if (
            payment.status == "awaiting_review"
            and payment.tx_hash
            and _coerce_utc(payment.created_at) < now - timedelta(hours=24)
        ):
            issues.append(
                ReconciliationIssue(
                    provider="usdt",
                    issue_type="usdt_stale_claim_waiting_review",
                    entity_type="payment",
                    entity_id=payment.id,
                    external_id=payment.tx_hash,
                    title="USDT tx hash is still awaiting review after 24h",
                    description="Retry explorer verification or resolve the manual-review reason.",
                    expected_state={"status": "succeeded, failed, or recent awaiting_review"},
                    observed_state=_payment_state(payment),
                )
            )

    for (network, tx_hash), rows in duplicate_claims.items():
        if len(rows) < 2:
            continue
        issues.append(
            ReconciliationIssue(
                provider="usdt",
                severity="critical",
                issue_type="usdt_duplicate_tx_claim",
                entity_type="payment",
                entity_id=",".join(str(row.id) for row in rows),
                external_id=tx_hash,
                title="USDT transaction hash is claimed by multiple payments",
                description="Launch anti-reuse must allow exactly one local payment per network+tx hash.",
                expected_state={"claim_count": 1, "network": network},
                observed_state={
                    "network": network,
                    "tx_hash": tx_hash,
                    "payment_ids": [row.id for row in rows],
                    "statuses": [row.status for row in rows],
                },
            )
        )

    return issues


def admin_reconciliation_url(run_id: int | None = None) -> str:
    base = settings.admin_base_url.rstrip("/") or "http://localhost:3000"
    suffix = "/reconciliation"
    if run_id is not None:
        suffix += f"?run={run_id}"
    return base + suffix


def reconciliation_summary_text(run: ReconciliationRun) -> str:
    """GK-430: lead with what changed, not with a running total.

    A number that only ever goes up is background noise within a week, and the
    one morning it means something is the morning nobody reads it. The count of
    conditions seen for the *first time* is the part that is actually news; the
    open total follows it as context.
    """
    open_count = run.open_items_count or 0
    new_count = int((run.summary or {}).get("new_items") or 0)
    if not open_count:
        state = "no open discrepancy items"
    elif new_count:
        state = f"{new_count} NEW discrepancy item(s), {open_count} open in total"
    else:
        state = f"nothing new, {open_count} open item(s) still unresolved"
    return (
        f"Reconciliation run #{run.id}: {state}. "
        f'<a href="{admin_reconciliation_url(run.id)}">Open admin page</a>'
    )


def _entity_key(entity_id: str | int | None) -> str | None:
    return str(entity_id)[:64] if entity_id is not None else None


def _issue_to_model(run_id: int, issue: ReconciliationIssue) -> ReconciliationItem:
    return ReconciliationItem(
        run_id=run_id,
        provider=issue.provider,
        severity=issue.severity,
        issue_type=issue.issue_type,
        entity_type=issue.entity_type,
        entity_id=_entity_key(issue.entity_id),
        external_id=issue.external_id,
        status="open",
        title=issue.title,
        description=issue.description,
        expected_state=_json_safe(issue.expected_state),
        observed_state=_json_safe(issue.observed_state),
    )


def _summary_for_issues(
    issues: list[ReconciliationIssue],
    *,
    errors: dict[str, str],
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    by_provider = {provider: 0 for provider in RECONCILIATION_PROVIDERS}
    by_severity = {"info": 0, "warning": 0, "critical": 0}
    for issue in issues:
        by_provider[issue.provider] = by_provider.get(issue.provider, 0) + 1
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
    summary = {
        "by_provider": by_provider,
        "by_severity": by_severity,
        "errors": errors,
    }
    if counts is not None:
        summary["new_items"] = counts.get("new", 0)
        summary["recurring_items"] = counts.get("recurring", 0)
        summary["carried_resolved_items"] = counts.get("carried_resolved", 0)
        summary["duplicates_dropped"] = counts.get("duplicates_dropped", 0)
    return summary


def _normalize_providers(raw: list[str] | tuple[str, ...] | None) -> list[str]:
    if not raw:
        return list(RECONCILIATION_PROVIDERS)
    normalized = []
    for provider in raw:
        value = provider.strip().lower()
        if value == "all":
            return list(RECONCILIATION_PROVIDERS)
        if value not in RECONCILIATION_PROVIDERS:
            raise ValueError(f"unsupported reconciliation provider: {provider}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def _unlinked_event_issue(
    provider: str,
    event: PaymentProviderEvent,
    expected_type: str,
) -> ReconciliationIssue:
    return ReconciliationIssue(
        provider=provider,
        severity="critical",
        issue_type=f"{provider}_event_unlinked",
        entity_type="provider_event",
        entity_id=event.id,
        external_id=event.event_id,
        title=f"{provider} {expected_type} event is not linked to a payment",
        description="Provider history exists, but no local payment row is tied to the event.",
        expected_state={"payment_id": "present"},
        observed_state=_event_state(event),
    )


def _remote_fetch_issue(
    provider: str,
    entity_type: str,
    external_id: str,
    exc: Exception,
) -> ReconciliationIssue:
    return ReconciliationIssue(
        provider=provider,
        issue_type=f"{provider}_{entity_type}_fetch_failed",
        entity_type=entity_type,
        external_id=external_id,
        title=f"{provider} {entity_type} fetch failed",
        description="Remote provider comparison could not read this object.",
        expected_state={"remote_fetch": "ok"},
        observed_state={"error": type(exc).__name__, "external_id": external_id},
    )


def _payment_state(payment: Payment) -> dict[str, Any]:
    return {
        "id": payment.id,
        "provider": payment.provider,
        "status": payment.status,
        "amount": payment.amount,
        "currency": payment.currency,
        "external_id": payment.external_id,
        "stripe_checkout_session_id": payment.stripe_checkout_session_id,
        "stripe_invoice_id": payment.stripe_invoice_id,
        "lava_invoice_id": payment.lava_invoice_id,
        "lava_subscription_id": payment.lava_subscription_id,
        "provider_event_id": payment.provider_event_id,
        "tx_hash": payment.tx_hash,
        "tx_network": payment.tx_network,
        "tx_confirmed_at": payment.tx_confirmed_at,
        "created_at": payment.created_at,
        "approved_at": payment.approved_at,
    }


def _subscription_state(sub: Subscription) -> dict[str, Any]:
    return {
        "id": sub.id,
        "user_id": sub.user_id,
        "status": sub.status,
        "source": sub.source,
        "provider": sub.provider,
        "provider_subscription_id": sub.provider_subscription_id,
        "provider_status": sub.provider_status,
        "current_period_start": sub.current_period_start,
        "current_period_end": sub.current_period_end,
        "expires_at": sub.expires_at,
        "grace_ends_at": sub.grace_ends_at,
        "cancel_at_period_end": sub.cancel_at_period_end,
        "access_revoked_at": sub.access_revoked_at,
    }


def _event_state(event: PaymentProviderEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "provider": event.provider,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "payment_id": event.payment_id,
        "processed_at": event.processed_at,
    }


def _stripe_object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_dict_recursive"):
        return _json_safe(value.to_dict_recursive())
    if isinstance(value, dict):
        return _json_safe(value)
    try:
        return _json_safe(dict(value))
    except (TypeError, ValueError):
        return {"id": getattr(value, "id", None), "status": getattr(value, "status", None)}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return _coerce_utc(value).isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _coerce_utc(value)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except (TypeError, ValueError):
        return Decimal("0")
