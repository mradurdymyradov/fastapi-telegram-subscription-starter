"""billing core: provider lifecycle fields and webhook dedupe table

Revision ID: 0003_billing_core
Revises: 0002_audit_log
Create Date: 2026-05-27 12:00:00

GK-010: extend User, Subscription, Payment with provider invoice/lifecycle/tx
fields; add `payment_provider_events` table for webhook idempotency.

This migration is purely additive — all new columns are nullable or carry a
server_default — so it applies cleanly on:
  - empty DB (initial bootstrap), and
  - seeded prototype DB (existing rows keep their values; new columns become
    NULL / their server_default for legacy rows).

No new Postgres ENUM types are introduced. provider/provider_status/tx_network
are plain VARCHAR; the canonical value set lives in the service layer.
"""
import sqlalchemy as sa

from alembic import op

revision = "0003_billing_core"
down_revision = "0002_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- users: Stripe customer link ----------------------------------------
    op.add_column(
        "users",
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_users_stripe_customer", "users", ["stripe_customer_id"]
    )
    op.create_index(
        "ix_users_stripe_customer", "users", ["stripe_customer_id"]
    )

    # --- subscriptions: provider lifecycle ----------------------------------
    op.add_column(
        "subscriptions",
        sa.Column("provider", sa.String(32), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("provider_subscription_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("provider_status", sa.String(32), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("grace_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sub_provider", "subscriptions", ["provider"])
    op.create_index(
        "ix_sub_provider_sub_id", "subscriptions", ["provider_subscription_id"]
    )
    op.create_index("ix_sub_provider_status", "subscriptions", ["provider_status"])

    # --- payments: provider invoice/session/event/tx ------------------------
    op.add_column(
        "payments",
        sa.Column("stripe_checkout_session_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("stripe_invoice_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("lava_invoice_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("lava_subscription_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("provider_event_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("tx_hash", sa.String(255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("tx_network", sa.String(16), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("tx_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column(
            "is_renewal",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "payments",
        sa.Column("billing_period_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("billing_period_end", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_payments_stripe_invoice", "payments", ["stripe_invoice_id"]
    )
    op.create_index(
        "ix_pay_stripe_session", "payments", ["stripe_checkout_session_id"]
    )
    op.create_index("ix_pay_lava_invoice", "payments", ["lava_invoice_id"])
    op.create_index("ix_pay_lava_subscription", "payments", ["lava_subscription_id"])
    op.create_index("ix_pay_provider_event", "payments", ["provider_event_id"])
    # GK-030 anti-reuse: (network, tx_hash) cannot be reclaimed. Partial unique
    # so legacy rows with NULL tx_hash do not collide.
    op.create_index(
        "uq_pay_tx_network_hash",
        "payments",
        ["tx_network", "tx_hash"],
        unique=True,
        postgresql_where=sa.text("tx_hash IS NOT NULL"),
    )

    # --- payment_provider_events: webhook idempotency -----------------------
    op.create_table(
        "payment_provider_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=True),
        sa.Column(
            "payment_id",
            sa.Integer,
            sa.ForeignKey("payments.id"),
            nullable=True,
        ),
        sa.Column("raw_hash", sa.String(64), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_payment_provider_event",
        "payment_provider_events",
        ["provider", "event_id"],
    )
    op.create_index(
        "ix_ppe_provider", "payment_provider_events", ["provider"]
    )
    op.create_index(
        "ix_ppe_event_type", "payment_provider_events", ["event_type"]
    )
    op.create_index(
        "ix_ppe_processed_at", "payment_provider_events", ["processed_at"]
    )


def downgrade() -> None:
    # --- payment_provider_events --------------------------------------------
    op.drop_index("ix_ppe_processed_at", table_name="payment_provider_events")
    op.drop_index("ix_ppe_event_type", table_name="payment_provider_events")
    op.drop_index("ix_ppe_provider", table_name="payment_provider_events")
    op.drop_constraint(
        "uq_payment_provider_event",
        "payment_provider_events",
        type_="unique",
    )
    op.drop_table("payment_provider_events")

    # --- payments -----------------------------------------------------------
    op.drop_index("uq_pay_tx_network_hash", table_name="payments")
    op.drop_index("ix_pay_provider_event", table_name="payments")
    op.drop_index("ix_pay_lava_subscription", table_name="payments")
    op.drop_index("ix_pay_lava_invoice", table_name="payments")
    op.drop_index("ix_pay_stripe_session", table_name="payments")
    op.drop_constraint("uq_payments_stripe_invoice", "payments", type_="unique")
    op.drop_column("payments", "billing_period_end")
    op.drop_column("payments", "billing_period_start")
    op.drop_column("payments", "is_renewal")
    op.drop_column("payments", "tx_confirmed_at")
    op.drop_column("payments", "tx_network")
    op.drop_column("payments", "tx_hash")
    op.drop_column("payments", "provider_event_id")
    op.drop_column("payments", "lava_subscription_id")
    op.drop_column("payments", "lava_invoice_id")
    op.drop_column("payments", "stripe_invoice_id")
    op.drop_column("payments", "stripe_checkout_session_id")

    # --- subscriptions ------------------------------------------------------
    op.drop_index("ix_sub_provider_status", table_name="subscriptions")
    op.drop_index("ix_sub_provider_sub_id", table_name="subscriptions")
    op.drop_index("ix_sub_provider", table_name="subscriptions")
    op.drop_column("subscriptions", "grace_ends_at")
    op.drop_column("subscriptions", "grace_started_at")
    op.drop_column("subscriptions", "cancel_at_period_end")
    op.drop_column("subscriptions", "current_period_end")
    op.drop_column("subscriptions", "current_period_start")
    op.drop_column("subscriptions", "provider_status")
    op.drop_column("subscriptions", "provider_subscription_id")
    op.drop_column("subscriptions", "provider")

    # --- users --------------------------------------------------------------
    op.drop_index("ix_users_stripe_customer", table_name="users")
    op.drop_constraint("uq_users_stripe_customer", "users", type_="unique")
    op.drop_column("users", "stripe_customer_id")
