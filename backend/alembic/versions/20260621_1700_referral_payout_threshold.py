"""Align the referral payout threshold default with the active partner model.

Revision ID: 0018_ref_payout_threshold
Revises: 0017_refund_lifecycle
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_ref_payout_threshold"
down_revision = "0017_refund_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "referral_payout_batches",
        "threshold_amount",
        existing_type=sa.Numeric(10, 2),
        server_default="100.00",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "referral_payout_batches",
        "threshold_amount",
        existing_type=sa.Numeric(10, 2),
        server_default="50.00",
        existing_nullable=False,
    )
