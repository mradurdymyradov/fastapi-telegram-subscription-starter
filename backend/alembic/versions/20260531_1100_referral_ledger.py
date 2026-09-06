"""referral commission ledger

Revision ID: 0005_referral_ledger
Revises: 0004_subscription_enforcement
Create Date: 2026-05-31 11:00:00

GK-020: replace launch referral rewards with a financial commission ledger
while preserving legacy bonus-day columns for historical rows.
"""
import sqlalchemy as sa

from alembic import op

revision = "0005_referral_ledger"
down_revision = "0004_subscription_enforcement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_payout_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column(
            "threshold_amount",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="50.00",
        ),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("commission_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'paid', 'cancelled')",
            name="ck_referral_payout_batches_status",
        ),
    )
    op.create_index(
        "ix_referral_payout_batches_status",
        "referral_payout_batches",
        ["status"],
    )
    op.create_index(
        "ix_referral_payout_batches_currency",
        "referral_payout_batches",
        ["currency"],
    )
    op.create_index(
        "ix_referral_payout_batches_created_at",
        "referral_payout_batches",
        ["created_at"],
    )

    op.create_table(
        "referral_commissions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("referral_id", sa.Integer, sa.ForeignKey("referrals.id"), nullable=False),
        sa.Column("referrer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referee_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source_payment_id", sa.Integer, sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("source_provider", sa.String(32), nullable=False),
        sa.Column("source_invoice_id", sa.String(255), nullable=True),
        sa.Column("source_provider_event_id", sa.String(255), nullable=True),
        sa.Column("source_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("source_currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("amount_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("vests_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text, nullable=True),
        sa.Column(
            "payout_batch_id",
            sa.Integer,
            sa.ForeignKey("referral_payout_batches.id"),
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
        sa.UniqueConstraint("referral_id", name="uq_referral_commissions_referral"),
        sa.UniqueConstraint("source_payment_id", name="uq_referral_commissions_payment"),
        sa.UniqueConstraint(
            "source_provider",
            "source_invoice_id",
            name="uq_referral_commissions_provider_invoice",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'vested', 'cancelled', 'paid')",
            name="ck_referral_commissions_status",
        ),
    )
    op.create_index("ix_referral_commissions_referral_id", "referral_commissions", ["referral_id"])
    op.create_index("ix_referral_commissions_referrer_id", "referral_commissions", ["referrer_id"])
    op.create_index("ix_referral_commissions_referee_id", "referral_commissions", ["referee_id"])
    op.create_index(
        "ix_referral_commissions_source_payment_id",
        "referral_commissions",
        ["source_payment_id"],
    )
    op.create_index(
        "ix_referral_commissions_source_provider",
        "referral_commissions",
        ["source_provider"],
    )
    op.create_index(
        "ix_referral_commissions_source_invoice_id",
        "referral_commissions",
        ["source_invoice_id"],
    )
    op.create_index(
        "ix_referral_commissions_source_provider_event_id",
        "referral_commissions",
        ["source_provider_event_id"],
    )
    op.create_index("ix_referral_commissions_status", "referral_commissions", ["status"])
    op.create_index("ix_referral_commissions_vests_at", "referral_commissions", ["vests_at"])
    op.create_index(
        "ix_referral_commissions_payout_batch_id",
        "referral_commissions",
        ["payout_batch_id"],
    )
    op.create_index(
        "ix_referral_commissions_created_at",
        "referral_commissions",
        ["created_at"],
    )

    op.create_table(
        "referral_adjustments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "commission_id",
            sa.Integer,
            sa.ForeignKey("referral_commissions.id"),
            nullable=False,
        ),
        sa.Column("amount_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
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
    )
    op.create_index(
        "ix_referral_adjustments_commission_id",
        "referral_adjustments",
        ["commission_id"],
    )
    op.create_index(
        "ix_referral_adjustments_created_at",
        "referral_adjustments",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_referral_adjustments_created_at", table_name="referral_adjustments")
    op.drop_index("ix_referral_adjustments_commission_id", table_name="referral_adjustments")
    op.drop_table("referral_adjustments")

    op.drop_index("ix_referral_commissions_created_at", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_payout_batch_id", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_vests_at", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_status", table_name="referral_commissions")
    op.drop_index(
        "ix_referral_commissions_source_provider_event_id",
        table_name="referral_commissions",
    )
    op.drop_index("ix_referral_commissions_source_invoice_id", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_source_provider", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_source_payment_id", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_referee_id", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_referrer_id", table_name="referral_commissions")
    op.drop_index("ix_referral_commissions_referral_id", table_name="referral_commissions")
    op.drop_table("referral_commissions")

    op.drop_index("ix_referral_payout_batches_created_at", table_name="referral_payout_batches")
    op.drop_index("ix_referral_payout_batches_currency", table_name="referral_payout_batches")
    op.drop_index("ix_referral_payout_batches_status", table_name="referral_payout_batches")
    op.drop_table("referral_payout_batches")
