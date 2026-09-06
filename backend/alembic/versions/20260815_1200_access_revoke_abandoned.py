"""Record that the bot gave up removing a member from Telegram.

Revision ID: 0026_access_revoke_abandoned
Revises: 0025_recon_condition_identity

GK-432: `sub#2` recorded **647** failed access-revoke attempts with
`access_revoked_at` still NULL. The bot's own log gave the cause on every one of
them — `can't remove chat owner` — and Telegram will never answer differently.
The hourly job retried both resources every hour since ~2026-07-10: 48 warning
lines a day, no alert, and a row that to any reader looked like an attempt still
in progress rather than one that will never finish.

`access_revoke_abandoned_at` is the terminal state. Set means: stop retrying,
a human has been told once, and the member is still in the channel. It is
deliberately separate from `access_revoked_at` (which asserts the opposite) and
from `status` (which describes entitlement, not Telegram) — the portal and the
access predicate are date-based and already deny these members.

Nothing is backfilled. The existing failures are re-attempted once after
deploy, classified, and abandoned on that attempt with the alert that says why.
"""
import sqlalchemy as sa

from alembic import op

revision = "0026_access_revoke_abandoned"
down_revision = "0025_recon_condition_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("access_revoke_abandoned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_subscriptions_access_revoke_abandoned_at",
        "subscriptions",
        ["access_revoke_abandoned_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscriptions_access_revoke_abandoned_at", table_name="subscriptions"
    )
    op.drop_column("subscriptions", "access_revoke_abandoned_at")
