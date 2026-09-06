"""recurring referral partner commissions

Revision ID: 0014_referral_partner
Revises: 0013_gift_activation_links
Create Date: 2026-06-20 12:00:00

GK-020: allow one commission per successful referred payment, preserve the
original 12-month earning window, and persist the invited-user retention gate.
"""
import sqlalchemy as sa

from alembic import op

revision = "0014_referral_partner"
down_revision = "0013_gift_activation_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "referrals",
        sa.Column("partner_earning_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("partner_earning_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("retention_streak_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("retention_coverage_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("retention_gate_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("retention_qualified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_referrals_partner_earning_started_at",
        "referrals",
        ["partner_earning_started_at"],
    )
    op.create_index(
        "ix_referrals_partner_earning_ends_at",
        "referrals",
        ["partner_earning_ends_at"],
    )
    op.create_index("ix_referrals_retention_gate_at", "referrals", ["retention_gate_at"])
    op.create_index(
        "ix_referrals_retention_qualified_at",
        "referrals",
        ["retention_qualified_at"],
    )

    op.drop_constraint(
        "uq_referral_commissions_referral",
        "referral_commissions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_referral_commissions_provider_event",
        "referral_commissions",
        ["source_provider", "source_provider_event_id"],
    )

    # Preserve the historical first commission as the earning-window anchor.
    # Current subscription coverage is preferred when the old ledger missed
    # renewals, otherwise fall back to the source payment's paid period/plan.
    op.execute(
        sa.text(
            """
            WITH referral_base AS (
                SELECT
                    r.id,
                    COALESCE(
                        p.billing_period_start,
                        p.approved_at,
                        p.created_at,
                        r.created_at
                    ) AS earning_started_at,
                    COALESCE(
                        (
                            SELECT MAX(COALESCE(s.current_period_end, s.expires_at))
                            FROM subscriptions s
                            WHERE s.user_id = r.referee_id
                        ),
                        p.billing_period_end,
                        COALESCE(
                            p.billing_period_start,
                            p.approved_at,
                            p.created_at,
                            r.created_at
                        ) + COALESCE(pl.duration_days, 0) * INTERVAL '1 day',
                        r.created_at
                    ) AS coverage_ends_at
                FROM referrals r
                LEFT JOIN payments p ON p.id = r.first_payment_id
                LEFT JOIN plans pl ON pl.id = p.plan_id
            )
            UPDATE referrals r
            SET
                partner_earning_started_at = b.earning_started_at,
                partner_earning_ends_at = b.earning_started_at + INTERVAL '12 months',
                retention_streak_started_at = b.earning_started_at,
                retention_coverage_ends_at = GREATEST(
                    b.earning_started_at,
                    b.coverage_ends_at
                ),
                retention_gate_at = b.earning_started_at + INTERVAL '3 months'
            FROM referral_base b
            WHERE r.id = b.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE referrals r
            SET retention_qualified_at = COALESCE(
                (
                    SELECT MIN(COALESCE(rc.vested_at, rc.paid_at, rc.vests_at))
                    FROM referral_commissions rc
                    WHERE rc.referral_id = r.id
                      AND rc.status IN ('vested', 'paid')
                ),
                r.retention_gate_at
            )
            WHERE EXISTS (
                SELECT 1
                FROM referral_commissions rc
                WHERE rc.referral_id = r.id
                  AND rc.status IN ('vested', 'paid')
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_referral_commissions_provider_event",
        "referral_commissions",
        type_="unique",
    )
    # The old schema permitted only one row per referral. Keep the earliest row
    # when explicitly downgrading so the legacy unique constraint can be restored.
    op.execute(
        sa.text(
            """
            DELETE FROM referral_commissions newer
            USING referral_commissions older
            WHERE newer.referral_id = older.referral_id
              AND newer.id > older.id
            """
        )
    )
    op.create_unique_constraint(
        "uq_referral_commissions_referral",
        "referral_commissions",
        ["referral_id"],
    )

    op.drop_index("ix_referrals_retention_qualified_at", table_name="referrals")
    op.drop_index("ix_referrals_retention_gate_at", table_name="referrals")
    op.drop_index("ix_referrals_partner_earning_ends_at", table_name="referrals")
    op.drop_index("ix_referrals_partner_earning_started_at", table_name="referrals")
    op.drop_column("referrals", "retention_qualified_at")
    op.drop_column("referrals", "retention_gate_at")
    op.drop_column("referrals", "retention_coverage_ends_at")
    op.drop_column("referrals", "retention_streak_started_at")
    op.drop_column("referrals", "partner_earning_ends_at")
    op.drop_column("referrals", "partner_earning_started_at")
