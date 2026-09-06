"""Record whether the newest backup was actually restorable.

Revision ID: 0023_backup_verifications
Revises: 0022_subscription_cancel_state

GK-437: GK-427 made backups real — encrypted, surviving a deploy, alerting on
a failed run. It could not answer the question that matters on the day you
need one: does this file still turn back into a database? On 2026-08-09 the
answer was no. The age private key on file failed its own checksum and every
dump taken until then was permanently unreadable, while the backup job kept
reporting success.

This table holds the verdict of the restore canary, one row per run, written
by ``deploy/backup/verify.sh``. Failures are rows too — a failed verification
must be visible rather than absent, because absence means the canary itself
died, and the bot's staleness job treats that as its own failure.
"""
import sqlalchemy as sa

from alembic import op

revision = "0023_backup_verifications"
down_revision = "0022_subscription_cancel_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_verifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("dump_file", sa.String(length=255), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("tables_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mismatches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_backup_verifications_verified_at", "backup_verifications", ["verified_at"]
    )
    op.create_index("ix_backup_verifications_ok", "backup_verifications", ["ok"])


def downgrade() -> None:
    op.drop_index("ix_backup_verifications_ok", table_name="backup_verifications")
    op.drop_index("ix_backup_verifications_verified_at", table_name="backup_verifications")
    op.drop_table("backup_verifications")
