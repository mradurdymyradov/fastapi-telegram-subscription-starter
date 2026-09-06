"""referral discount reservations

Revision ID: 0020_ref_discount_reservation
Revises: 0019_launch_plan_prices
Create Date: 2026-07-04 10:00:00

GK-402 (security Finding 5): make the one-time referral discount a single durable
reservation so a referred user cannot complete more than one discounted checkout.
``referral_discount_reservations`` records the reservation created at
discounted-checkout creation; a *partial* unique index guarantees at most one
``active`` or ``consumed`` row per user, while ``released`` (abandoned/expired)
rows never block a fresh reservation. Plain VARCHAR + CheckConstraint for the
small status enum (CLAUDE.md enum rule), so there is no Postgres ENUM to churn.
"""
import sqlalchemy as sa

from alembic import op

revision = "0020_ref_discount_reservation"
down_revision = "0019_launch_plan_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_discount_reservations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referrer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("payment_id", sa.Integer, sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("plan_code", sa.String(32), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("original_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("final_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'consumed', 'released')",
            name="ck_referral_discount_reservations_status",
        ),
    )
    op.create_index(
        "ix_referral_discount_reservations_user_id",
        "referral_discount_reservations",
        ["user_id"],
    )
    op.create_index(
        "ix_referral_discount_reservations_referrer_id",
        "referral_discount_reservations",
        ["referrer_id"],
    )
    op.create_index(
        "ix_referral_discount_reservations_payment_id",
        "referral_discount_reservations",
        ["payment_id"],
    )
    op.create_index(
        "ix_referral_discount_reservations_status",
        "referral_discount_reservations",
        ["status"],
    )
    op.create_index(
        "ix_referral_discount_reservations_created_at",
        "referral_discount_reservations",
        ["created_at"],
    )
    # GK-402 single-use guarantee: at most one live/spent referral discount per
    # user. Partial so abandoned/expired ('released') rows do not block re-use.
    op.create_index(
        "uq_referral_discount_active_user",
        "referral_discount_reservations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'consumed')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_referral_discount_active_user",
        table_name="referral_discount_reservations",
    )
    op.drop_index(
        "ix_referral_discount_reservations_created_at",
        table_name="referral_discount_reservations",
    )
    op.drop_index(
        "ix_referral_discount_reservations_status",
        table_name="referral_discount_reservations",
    )
    op.drop_index(
        "ix_referral_discount_reservations_payment_id",
        table_name="referral_discount_reservations",
    )
    op.drop_index(
        "ix_referral_discount_reservations_referrer_id",
        table_name="referral_discount_reservations",
    )
    op.drop_index(
        "ix_referral_discount_reservations_user_id",
        table_name="referral_discount_reservations",
    )
    op.drop_table("referral_discount_reservations")
