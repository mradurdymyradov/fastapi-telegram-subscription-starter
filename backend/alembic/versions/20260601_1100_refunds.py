"""refunds (GK-200)

Revision ID: 0011_refunds
Revises: 0010_admin_totp
Create Date: 2026-06-01 11:00:00

GK-200: provider-aware refund workflow. Purely additive:
- payments.stripe_payment_intent_id — lets a Stripe refund target the charge by
  payment intent without re-fetching the invoice.
- payments.refunded_amount — running total of succeeded refunds; distinguishes
  partial (0 < refunded < amount, status stays 'succeeded') from full (status
  flips to 'refunded').
- refunds table — one row per recorded refund (Stripe auto, or manual for
  USDT/Zelle and gated Lava), with full audit trail and the referral commission
  action taken.

Statuses stay plain VARCHAR with CHECK constraints; no new Postgres ENUMs
(CLAUDE.md alembic enum rule).
"""
import sqlalchemy as sa

from alembic import op

revision = "0011_refunds"
down_revision = "0010_admin_totp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("stripe_payment_intent_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_payments_stripe_payment_intent_id",
        "payments",
        ["stripe_payment_intent_id"],
    )
    op.add_column(
        "payments",
        sa.Column("refunded_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )

    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("payment_id", sa.Integer, sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("refund_type", sa.String(16), nullable=False, server_default="full"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("provider_refund_id", sa.String(255), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("commission_action", sa.String(16), nullable=True),
        sa.Column("commission_adjustment_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "created_by_admin_id",
            sa.Integer,
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider", "provider_refund_id", name="uq_refunds_provider_refund_id"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name="ck_refunds_status",
        ),
        sa.CheckConstraint(
            "refund_type IN ('full', 'partial')",
            name="ck_refunds_type",
        ),
    )
    op.create_index("ix_refunds_payment_id", "refunds", ["payment_id"])
    op.create_index("ix_refunds_provider", "refunds", ["provider"])
    op.create_index("ix_refunds_status", "refunds", ["status"])
    op.create_index("ix_refunds_created_at", "refunds", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_refunds_created_at", table_name="refunds")
    op.drop_index("ix_refunds_status", table_name="refunds")
    op.drop_index("ix_refunds_provider", table_name="refunds")
    op.drop_index("ix_refunds_payment_id", table_name="refunds")
    op.drop_table("refunds")

    op.drop_column("payments", "refunded_amount")
    op.drop_index("ix_payments_stripe_payment_intent_id", table_name="payments")
    op.drop_column("payments", "stripe_payment_intent_id")
