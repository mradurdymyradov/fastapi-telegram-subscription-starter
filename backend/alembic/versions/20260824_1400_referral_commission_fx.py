"""Record the rate a commission was converted at.

Revision ID: 0028_referral_commission_fx
Revises: 0027_subscription_comp

GK-457. `referral_commissions.amount_usd` is summed, thresholded at $100 and paid
out as dollars, but it was being written straight from `payment.amount` whatever
currency that was — so a 1500 ₽ Lava payment wrote 300 into it. The row already
knew better: `source_currency` beside it said RUB the whole time.

Converting is only half a fix. A converted figure with no record of the rate is
un-auditable — nobody can tell a correct conversion from a wrong one, or explain
a partner's total back to them — so the rate goes on the row.

Nullable, and deliberately not backfilled for non-USD rows. Filling them in would
mean this migration rewriting money at a rate nobody reviewed, and the rows are
wrong in a way that a number cannot express: their `amount_usd` is not a
mis-converted dollar figure, it is a rouble figure. They are left standing, and
findable:

    SELECT id, referrer_id, source_currency, source_amount, amount_usd, status
      FROM referral_commissions
     WHERE fx_rate_to_usd IS NULL AND source_currency <> 'USD';

USD rows are backfilled to 1 because that is not a judgement call — a dollar has
always been a dollar, and leaving them NULL would bury the rows above in noise.
"""
import sqlalchemy as sa

from alembic import op

revision = "0028_referral_commission_fx"
down_revision = "0027_subscription_comp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "referral_commissions",
        sa.Column("fx_rate_to_usd", sa.Numeric(18, 8), nullable=True),
    )
    op.execute(
        """
        UPDATE referral_commissions
           SET fx_rate_to_usd = 1
         WHERE source_currency = 'USD'
        """
    )


def downgrade() -> None:
    op.drop_column("referral_commissions", "fx_rate_to_usd")
