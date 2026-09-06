"""promo codes and redemptions

Revision ID: 0012_promo_codes
Revises: 0011_refunds
Create Date: 2026-06-02 12:00:00

GK-210: rich promo-code management. ``promo_codes`` are admin-created coupons
with their own discount, plan scope, usage cap, and validity window;
``promo_redemptions`` record one use per (code, user) and link a redemption to
the Payment it discounted. Plain VARCHAR + CheckConstraint for the small enums
(CLAUDE.md enum rule), so there is no Postgres ENUM type to churn.
"""
import sqlalchemy as sa

from alembic import op

revision = "0012_promo_codes"
down_revision = "0011_refunds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("discount_type", sa.String(16), nullable=False, server_default="percent"),
        sa.Column("percent_off", sa.Numeric(5, 2), nullable=True),
        sa.Column("amount_off", sa.Numeric(10, 2), nullable=True),
        sa.Column("amount_off_currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("applies_to_plan_codes", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("max_redemptions", sa.Integer, nullable=True),
        sa.Column("redeemed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("referrer_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_by_admin_id", sa.Integer, sa.ForeignKey("admin_users.id"), nullable=True
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
        sa.UniqueConstraint("code", name="uq_promo_codes_code"),
        sa.CheckConstraint(
            "discount_type IN ('percent', 'fixed')",
            name="ck_promo_codes_discount_type",
        ),
    )
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"])
    op.create_index("ix_promo_codes_is_active", "promo_codes", ["is_active"])
    op.create_index("ix_promo_codes_referrer_user_id", "promo_codes", ["referrer_user_id"])
    op.create_index("ix_promo_codes_created_at", "promo_codes", ["created_at"])

    op.create_table(
        "promo_redemptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "promo_code_id", sa.Integer, sa.ForeignKey("promo_codes.id"), nullable=False
        ),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payment_id", sa.Integer, sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="applied"),
        sa.Column("plan_code", sa.String(32), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("original_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("final_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "promo_code_id", "user_id", name="uq_promo_redemptions_code_user"
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'cancelled')",
            name="ck_promo_redemptions_status",
        ),
    )
    op.create_index("ix_promo_redemptions_promo_code_id", "promo_redemptions", ["promo_code_id"])
    op.create_index("ix_promo_redemptions_user_id", "promo_redemptions", ["user_id"])
    op.create_index("ix_promo_redemptions_payment_id", "promo_redemptions", ["payment_id"])
    op.create_index("ix_promo_redemptions_status", "promo_redemptions", ["status"])
    op.create_index("ix_promo_redemptions_created_at", "promo_redemptions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_promo_redemptions_created_at", table_name="promo_redemptions")
    op.drop_index("ix_promo_redemptions_status", table_name="promo_redemptions")
    op.drop_index("ix_promo_redemptions_payment_id", table_name="promo_redemptions")
    op.drop_index("ix_promo_redemptions_user_id", table_name="promo_redemptions")
    op.drop_index("ix_promo_redemptions_promo_code_id", table_name="promo_redemptions")
    op.drop_table("promo_redemptions")

    op.drop_index("ix_promo_codes_created_at", table_name="promo_codes")
    op.drop_index("ix_promo_codes_referrer_user_id", table_name="promo_codes")
    op.drop_index("ix_promo_codes_is_active", table_name="promo_codes")
    op.drop_index("ix_promo_codes_code", table_name="promo_codes")
    op.drop_table("promo_codes")
