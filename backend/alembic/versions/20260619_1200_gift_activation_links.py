"""gift activation links

Revision ID: 0013_gift_activation_links
Revises: 0012_promo_codes
Create Date: 2026-06-19 12:00:00

"""
from alembic import op

revision = "0013_gift_activation_links"
down_revision = "0012_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("gifts", "receiver_id", nullable=True)
    op.create_unique_constraint("uq_gifts_payment", "gifts", ["payment_id"])


def downgrade() -> None:
    op.drop_constraint("uq_gifts_payment", "gifts", type_="unique")
    op.execute("UPDATE gifts SET receiver_id = sender_id WHERE receiver_id IS NULL")
    op.alter_column("gifts", "receiver_id", nullable=False)
