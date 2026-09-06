"""archive module showcase cover

Revision ID: 0016_archive_module_cover
Revises: 0015_support_delivery_status
Create Date: 2026-06-21 09:00:00

GK-383: persist the Vimeo showcase's own cover so portal module cards do not
silently borrow an inner lesson thumbnail. Nullable for existing/manual modules;
the portal retains the first-visible-video fallback when Vimeo has no cover.
"""
import sqlalchemy as sa

from alembic import op

revision = "0016_archive_module_cover"
down_revision = "0015_support_delivery_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "archive_modules",
        sa.Column("cover_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("archive_modules", "cover_url")
