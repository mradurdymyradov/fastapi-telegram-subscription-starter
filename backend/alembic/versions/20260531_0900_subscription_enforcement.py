"""subscription enforcement side-effect tracking

Revision ID: 0004_subscription_enforcement
Revises: 0003_billing_core
Create Date: 2026-05-31 09:00:00

GK-014: track Telegram access-revoke attempts so the scheduler can honor
provider grace windows and survive restarts without blindly re-processing users.
"""
import sqlalchemy as sa

from alembic import op

revision = "0004_subscription_enforcement"
down_revision = "0003_billing_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("access_revoke_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("access_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("access_revoke_retry_after_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "access_revoke_attempts",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("access_revoke_error", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_sub_access_revoke_retry_after",
        "subscriptions",
        ["access_revoke_retry_after_at"],
    )
    op.create_index(
        "ix_sub_access_revoked_at",
        "subscriptions",
        ["access_revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sub_access_revoked_at", table_name="subscriptions")
    op.drop_index("ix_sub_access_revoke_retry_after", table_name="subscriptions")
    op.drop_column("subscriptions", "access_revoke_error")
    op.drop_column("subscriptions", "access_revoke_attempts")
    op.drop_column("subscriptions", "access_revoke_retry_after_at")
    op.drop_column("subscriptions", "access_revoked_at")
    op.drop_column("subscriptions", "access_revoke_attempted_at")
