"""Release the provider subscription link from superseded subscription rows.

Revision ID: 0024_superseded_provider_links
Revises: 0023_backup_verifications

GK-426: a renewal that arrives after access has lapsed creates a **new**
subscription row while the old one keeps the same ``provider_subscription_id``
and the ``provider_status`` it was frozen at. Reconciliation compares that dead
row against live provider state and reports a critical for it — every night,
forever. Confirmed on live data 2026-08-06: ``sub#2``/``sub#8`` (user 3) both
carry ``sub_1Tgq8ZRY…``; ``sub#5``/``sub#11`` (user 7) both carry
``sub_1TliuwRY…``. Four of the ten standing items traced to exactly this.

``release_superseded_provider_link`` stops new ones being made. This releases
the ones that already exist: for every provider subscription id claimed by more
than one row, the newest row (``expires_at`` desc, newest id as tie-break —
the same rule the webhook lookups use) keeps the link and the rest give it up.

Data-only and idempotent: rerunning it selects the same winner and finds
nothing left to clear. Not reversible in a meaningful sense — the downgrade
cannot know which cleared row held which id — so it is deliberately a no-op
rather than a lie.
"""
from alembic import op

revision = "0024_superseded_provider_links"
down_revision = "0023_backup_verifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE subscriptions
           SET provider_subscription_id = NULL,
               provider_status = NULL
         WHERE provider_subscription_id IS NOT NULL
           AND id NOT IN (
               SELECT DISTINCT ON (provider_subscription_id) id
                 FROM subscriptions
                WHERE provider_subscription_id IS NOT NULL
                ORDER BY provider_subscription_id, expires_at DESC, id DESC
           )
        """
    )


def downgrade() -> None:
    """Intentionally empty — the released ids are not recoverable from this table.

    They are still on the ``payments`` rows if a reconstruction is ever needed.
    """
