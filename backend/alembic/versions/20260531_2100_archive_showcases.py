"""vimeo showcases → archive modules (hybrid auto + admin override)

Revision ID: 0009_archive_showcases
Revises: 0008_reconciliation
Create Date: 2026-05-31 21:00:00

GK-092: seed archive modules from Vimeo Showcases (albums) and model video↔module
membership as many-to-many. Purely additive:

- `archive_modules.vimeo_album_id` (unique, nullable) — links a module to its Vimeo
  showcase; NULL ⇒ admin-created module the sync never touches.
- `archive_modules.vimeo_synced` (JSON) — last-synced Vimeo field snapshot for the
  3-way merge that preserves admin renames.
- `archive_video_modules` — the M2M link table with `source` ('vimeo'|'admin') and a
  `removed_by_admin` tombstone so admin add/remove edits survive re-sync.

No Postgres ENUM is created (`source` is plain VARCHAR validated in the service
layer, per the CLAUDE.md enum rule).
"""
import sqlalchemy as sa

from alembic import op

revision = "0009_archive_showcases"
down_revision = "0008_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "archive_modules",
        sa.Column("vimeo_album_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "archive_modules",
        sa.Column("vimeo_synced", sa.JSON, nullable=True),
    )
    # Unique so re-sync upserts the same module in place. Nullable column → Postgres
    # allows multiple NULLs, so admin-created modules don't collide.
    op.create_index(
        "ix_archive_modules_vimeo_album_id",
        "archive_modules",
        ["vimeo_album_id"],
        unique=True,
    )

    op.create_table(
        "archive_video_modules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "video_id",
            sa.Integer,
            sa.ForeignKey("archive_videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "module_id",
            sa.Integer,
            sa.ForeignKey("archive_modules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False, server_default="vimeo"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "removed_by_admin",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("video_id", "module_id", name="uq_archive_video_modules_pair"),
    )
    op.create_index(
        "ix_archive_video_modules_video_id", "archive_video_modules", ["video_id"]
    )
    op.create_index(
        "ix_archive_video_modules_module_id", "archive_video_modules", ["module_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_archive_video_modules_module_id", table_name="archive_video_modules")
    op.drop_index("ix_archive_video_modules_video_id", table_name="archive_video_modules")
    op.drop_table("archive_video_modules")

    op.drop_index("ix_archive_modules_vimeo_album_id", table_name="archive_modules")
    op.drop_column("archive_modules", "vimeo_synced")
    op.drop_column("archive_modules", "vimeo_album_id")
