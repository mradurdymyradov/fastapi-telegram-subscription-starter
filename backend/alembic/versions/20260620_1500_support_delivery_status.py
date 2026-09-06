"""support message delivery/routing status

Revision ID: 0015_support_delivery_status
Revises: 0014_referral_partner
Create Date: 2026-06-20 15:00:00

GK-378: persist the curator-routing outcome for inbound user support messages
and the user-delivery outcome for admin replies, so the admin panel can show
delivery state and routing failures are auditable. Nullable so existing rows
(and the role="user" cancel-request tickets) stay valid without a backfill.
"""
import sqlalchemy as sa

from alembic import op

revision = "0015_support_delivery_status"
down_revision = "0014_referral_partner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "support_messages",
        sa.Column("delivery_status", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("support_messages", "delivery_status")
