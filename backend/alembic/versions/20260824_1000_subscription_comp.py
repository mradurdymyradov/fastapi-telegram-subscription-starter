"""A subscription the community is not being paid for.

Revision ID: 0027_subscription_comp
Revises: 0026_access_revoke_abandoned

GK-483. Grant, 2026-08-23: «Нас оставь и в чате, и в админ-панели, но отдельным
статусом, не как платящих». No existing column can carry that. `status='active'`
puts the team straight into `active_subs`; `source='gift'` means somebody paid;
a fourth `sub_status` value would drop the row out of `ACCESS_HOLDING_STATUSES`
and so mean "no access to revoke", which is the opposite of «оставь в чате».

`is_comp` is a boolean beside the status, not a status: the row stays `active`,
so invites, revocation and the portal predicate all keep working unchanged,
while every count that means "paying members" filters it out.

NOT NULL DEFAULT false, and the server default is kept rather than dropped after
the backfill — rows are inserted from three different code paths plus one-shot
scripts, and a column that is NOT NULL with no server default turns any INSERT
that forgets it into an error at 3am. Every existing row is a real subscription,
so the backfill is the default itself; nothing is reclassified by this migration.
"""
import sqlalchemy as sa

from alembic import op

revision = "0027_subscription_comp"
down_revision = "0026_access_revoke_abandoned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "is_comp",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Partial: the team is a handful of rows next to every subscription ever
    # sold, and the only query that wants them is "show me the comp rows".
    op.create_index(
        "ix_subscriptions_is_comp",
        "subscriptions",
        ["is_comp"],
        postgresql_where=sa.text("is_comp"),
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_is_comp", table_name="subscriptions")
    op.drop_column("subscriptions", "is_comp")
