"""Update launch plan prices for the final Lava/Stripe offer.

Revision ID: 0019_launch_plan_prices
Revises: 0018_ref_payout_threshold
"""

from alembic import op

revision = "0019_launch_plan_prices"
down_revision = "0018_ref_payout_threshold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE plans
        SET price_usd = CASE code
                WHEN '1m' THEN 19.00
                WHEN '6m' THEN 79.00
                WHEN '12m' THEN 129.00
            END,
            price_rub = CASE code
                WHEN '1m' THEN 1500.00
                WHEN '6m' THEN 7000.00
                WHEN '12m' THEN 10000.00
            END
        WHERE code IN ('1m', '6m', '12m')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE plans
        SET price_usd = CASE code
                WHEN '1m' THEN 19.00
                WHEN '6m' THEN 89.00
                WHEN '12m' THEN 149.00
            END,
            price_rub = 0.00
        WHERE code IN ('1m', '6m', '12m')
        """
    )
