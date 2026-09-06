"""refund lifecycle and confirmed-accounting gate

Revision ID: 0017_refund_lifecycle
Revises: 0016_archive_module_cover
Create Date: 2026-06-21 13:00:00

GK-382: unconfirmed refund requests must not reduce recognized cash, revoke
access, or adjust partner commissions. Existing succeeded refund rows are
backfilled as provider-confirmed/accounting-applied so historical totals remain
unchanged.
"""
import sqlalchemy as sa

from alembic import op

revision = "0017_refund_lifecycle"
down_revision = "0016_archive_module_cover"
branch_labels = None
depends_on = None


_ACTIVE = "status IN ('requested', 'pending', 'manual_action_required')"


def upgrade() -> None:
    op.drop_constraint("ck_refunds_status", "refunds", type_="check")
    op.alter_column(
        "refunds",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
        server_default="requested",
    )
    op.add_column("refunds", sa.Column("request_key", sa.String(128), nullable=True))
    op.add_column("refunds", sa.Column("provider_status", sa.String(32), nullable=True))
    op.add_column("refunds", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column(
        "refunds",
        sa.Column(
            "confirmed_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
    )
    op.add_column("refunds", sa.Column("confirmation_note", sa.Text(), nullable=True))
    op.add_column(
        "refunds", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "refunds",
        sa.Column("accounting_applied_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        UPDATE refunds
        SET status = 'provider_confirmed',
            provider_status = COALESCE(provider_status, 'succeeded'),
            confirmed_by_admin_id = created_by_admin_id,
            confirmed_at = COALESCE(updated_at, created_at),
            accounting_applied_at = COALESCE(updated_at, created_at)
        WHERE status = 'succeeded'
        """
    )
    # Older code allowed more than one pending row for the same payment. Keep
    # the newest unresolved request active and close older duplicates before
    # adding the partial unique index.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY payment_id
                       ORDER BY created_at DESC, id DESC
                   ) AS active_rank
            FROM refunds
            WHERE status = 'pending'
        )
        UPDATE refunds AS r
        SET status = 'failed',
            provider_status = COALESCE(r.provider_status, 'legacy_duplicate'),
            failure_reason = COALESCE(
                r.failure_reason,
                'Closed during GK-382 migration: a newer unresolved refund exists.'
            )
        FROM ranked
        WHERE r.id = ranked.id AND ranked.active_rank > 1
        """
    )
    op.create_check_constraint(
        "ck_refunds_status",
        "refunds",
        "status IN ('requested', 'pending', 'provider_confirmed', "
        "'failed', 'manual_action_required')",
    )
    op.create_unique_constraint("uq_refunds_request_key", "refunds", ["request_key"])
    op.create_index(
        "uq_refunds_one_active_per_payment",
        "refunds",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE),
    )


def downgrade() -> None:
    op.drop_index("uq_refunds_one_active_per_payment", table_name="refunds")
    op.drop_constraint("uq_refunds_request_key", "refunds", type_="unique")
    op.drop_constraint("ck_refunds_status", "refunds", type_="check")
    op.execute(
        """
        UPDATE refunds
        SET status = CASE
            WHEN status = 'provider_confirmed' THEN 'succeeded'
            WHEN status IN ('requested', 'manual_action_required') THEN 'pending'
            ELSE status
        END
        """
    )
    op.create_check_constraint(
        "ck_refunds_status",
        "refunds",
        "status IN ('pending', 'succeeded', 'failed')",
    )
    op.drop_column("refunds", "accounting_applied_at")
    op.drop_column("refunds", "confirmed_at")
    op.drop_column("refunds", "confirmation_note")
    op.drop_column("refunds", "confirmed_by_admin_id")
    op.drop_column("refunds", "failure_reason")
    op.drop_column("refunds", "provider_status")
    op.drop_column("refunds", "request_key")
    op.alter_column(
        "refunds",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
        server_default="pending",
    )
