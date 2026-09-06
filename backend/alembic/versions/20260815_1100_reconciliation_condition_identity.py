"""Give a reconciliation finding an identity that outlives its run.

Revision ID: 0025_recon_condition_identity
Revises: 0024_superseded_provider_links

GK-430: 312 items had ever been created and 6 had ever been resolved, across
62 nightly runs. Not because nobody looked — because resolving an item only
silenced that one row, in that one run, and the same condition came back as a
fresh open row the next morning. A report that can only go up is a report
nobody reads.

Two changes, both about identifying the *condition* rather than the row:

- an index on (provider, issue_type, entity_type, entity_id), which is what a
  condition is, so the "newest row per condition" lookup each run is cheap;
- ``first_seen_at``, carried forward when a condition is re-detected, so the
  panel can say "standing since June" instead of "created last night" — and so
  the nightly alert can report what is genuinely new.

The backfill sets ``first_seen_at`` to the earliest ``created_at`` of each
condition rather than to the migration timestamp, because the honest answer for
the existing rows is months ago, not today.
"""
import sqlalchemy as sa

from alembic import op

revision = "0025_recon_condition_identity"
down_revision = "0024_superseded_provider_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reconciliation_items",
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # `IS NOT DISTINCT FROM` because entity_id is nullable and NULL = NULL is
    # never true — without it every row with no entity would miss its own group.
    op.execute(
        """
        UPDATE reconciliation_items AS i
           SET first_seen_at = c.first_seen
          FROM (
              SELECT provider,
                     issue_type,
                     entity_type,
                     entity_id,
                     MIN(created_at) AS first_seen
                FROM reconciliation_items
               GROUP BY provider, issue_type, entity_type, entity_id
          ) AS c
         WHERE i.provider = c.provider
           AND i.issue_type = c.issue_type
           AND i.entity_type = c.entity_type
           AND i.entity_id IS NOT DISTINCT FROM c.entity_id
        """
    )
    op.create_index(
        "ix_reconciliation_items_first_seen_at", "reconciliation_items", ["first_seen_at"]
    )
    op.create_index(
        "ix_reconciliation_items_condition",
        "reconciliation_items",
        ["provider", "issue_type", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_items_condition", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_first_seen_at", table_name="reconciliation_items")
    op.drop_column("reconciliation_items", "first_seen_at")
