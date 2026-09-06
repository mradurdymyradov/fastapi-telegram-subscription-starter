"""referral first-touch attribution

Revision ID: 0006_referral_attribution
Revises: 0005_referral_ledger
Create Date: 2026-05-31 15:00:00

GK-021: persist immutable pre-payment referral attribution, keep a source
marker for future promo-code attribution, and expose ignored second-referrer
attempts for manual review.
"""
import sqlalchemy as sa

from alembic import op

revision = "0006_referral_attribution"
down_revision = "0005_referral_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_attributions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("referrer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referee_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="telegram_deeplink",
        ),
        sa.Column("code", sa.String(64), nullable=True),
        sa.Column(
            "attributed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="clear"),
        sa.Column("suspicious_reason", sa.Text, nullable=True),
        sa.Column(
            "ignored_attempt_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_ignored_referrer_id",
            sa.Integer,
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("last_ignored_source", sa.String(32), nullable=True),
        sa.Column("last_ignored_code", sa.String(64), nullable=True),
        sa.Column("last_ignored_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("referee_id", name="uq_referral_attributions_referee"),
        sa.CheckConstraint(
            "source IN ('telegram_deeplink', 'promo_code', 'admin', 'legacy')",
            name="ck_referral_attributions_source",
        ),
        sa.CheckConstraint(
            "review_status IN ('clear', 'suspicious', 'dismissed')",
            name="ck_referral_attributions_review_status",
        ),
    )
    op.create_index(
        "ix_referral_attributions_referrer_id",
        "referral_attributions",
        ["referrer_id"],
    )
    op.create_index(
        "ix_referral_attributions_referee_id",
        "referral_attributions",
        ["referee_id"],
    )
    op.create_index("ix_referral_attributions_source", "referral_attributions", ["source"])
    op.create_index(
        "ix_referral_attributions_attributed_at",
        "referral_attributions",
        ["attributed_at"],
    )
    op.create_index(
        "ix_referral_attributions_review_status",
        "referral_attributions",
        ["review_status"],
    )
    op.create_index(
        "ix_referral_attributions_last_ignored_referrer_id",
        "referral_attributions",
        ["last_ignored_referrer_id"],
    )

    # Preserve any prototype/demo rows that already have the legacy User.referrer_id
    # first-touch marker before this table existed.
    op.execute(
        sa.text(
            """
            INSERT INTO referral_attributions (
                referrer_id,
                referee_id,
                source,
                attributed_at,
                review_status,
                ignored_attempt_count
            )
            SELECT referrer_id, id, 'legacy', joined_at, 'clear', 0
            FROM users
            WHERE referrer_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_referral_attributions_last_ignored_referrer_id",
        table_name="referral_attributions",
    )
    op.drop_index("ix_referral_attributions_review_status", table_name="referral_attributions")
    op.drop_index("ix_referral_attributions_attributed_at", table_name="referral_attributions")
    op.drop_index("ix_referral_attributions_source", table_name="referral_attributions")
    op.drop_index("ix_referral_attributions_referee_id", table_name="referral_attributions")
    op.drop_index("ix_referral_attributions_referrer_id", table_name="referral_attributions")
    op.drop_table("referral_attributions")
