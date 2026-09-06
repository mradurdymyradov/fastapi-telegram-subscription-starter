"""vimeo-backed member portal

Revision ID: 0007_portal
Revises: 0006_referral_attribution
Create Date: 2026-05-31 17:00:00

GK-091: archive metadata mirror (`archive_modules`, `archive_videos`) plus the
portal auth surface (`portal_magic_links`, `portal_sessions`). Purely additive —
no existing table is touched and no Postgres ENUM is created (visibility/privacy
are plain VARCHAR validated in the service layer, per the CLAUDE.md enum rule).
"""
import sqlalchemy as sa

from alembic import op

revision = "0007_portal"
down_revision = "0006_referral_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "archive_modules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
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
        sa.UniqueConstraint("code", name="uq_archive_modules_code"),
    )

    op.create_table(
        "archive_videos",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("vimeo_id", sa.BigInteger, nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("thumbnail_url", sa.String(512), nullable=True),
        sa.Column(
            "module_id",
            sa.Integer,
            sa.ForeignKey("archive_modules.id"),
            nullable=True,
        ),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="visible"),
        sa.Column("vimeo_privacy", sa.String(32), nullable=True),
        sa.Column("player_embed_url", sa.String(512), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_archive_videos_vimeo_id", "archive_videos", ["vimeo_id"], unique=True
    )
    op.create_index("ix_archive_videos_module_id", "archive_videos", ["module_id"])
    op.create_index("ix_archive_videos_visibility", "archive_videos", ["visibility"])
    # Portal listing orders within a module by sort_order — back it with an index.
    op.create_index(
        "ix_archive_videos_module_sort", "archive_videos", ["module_id", "sort_order"]
    )

    op.create_table(
        "portal_magic_links",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_portal_magic_links_token_hash",
        "portal_magic_links",
        ["token_hash"],
        unique=True,
    )
    op.create_index("ix_portal_magic_links_user_id", "portal_magic_links", ["user_id"])
    op.create_index(
        "ix_portal_magic_links_expires_at", "portal_magic_links", ["expires_at"]
    )
    op.create_index(
        "ix_portal_magic_links_user_expires",
        "portal_magic_links",
        ["user_id", "expires_at"],
    )

    op.create_table(
        "portal_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent_hash", sa.String(64), nullable=True),
        sa.Column("ip_first_seen", sa.String(64), nullable=True),
        sa.Column("ip_last_seen", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_portal_sessions_token_hash", "portal_sessions", ["token_hash"], unique=True
    )
    op.create_index("ix_portal_sessions_user_id", "portal_sessions", ["user_id"])
    op.create_index("ix_portal_sessions_expires_at", "portal_sessions", ["expires_at"])
    op.create_index(
        "ix_portal_sessions_user_expires_revoked",
        "portal_sessions",
        ["user_id", "expires_at", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_portal_sessions_user_expires_revoked", table_name="portal_sessions")
    op.drop_index("ix_portal_sessions_expires_at", table_name="portal_sessions")
    op.drop_index("ix_portal_sessions_user_id", table_name="portal_sessions")
    op.drop_index("ix_portal_sessions_token_hash", table_name="portal_sessions")
    op.drop_table("portal_sessions")

    op.drop_index("ix_portal_magic_links_user_expires", table_name="portal_magic_links")
    op.drop_index("ix_portal_magic_links_expires_at", table_name="portal_magic_links")
    op.drop_index("ix_portal_magic_links_user_id", table_name="portal_magic_links")
    op.drop_index("ix_portal_magic_links_token_hash", table_name="portal_magic_links")
    op.drop_table("portal_magic_links")

    op.drop_index("ix_archive_videos_module_sort", table_name="archive_videos")
    op.drop_index("ix_archive_videos_visibility", table_name="archive_videos")
    op.drop_index("ix_archive_videos_module_id", table_name="archive_videos")
    op.drop_index("ix_archive_videos_vimeo_id", table_name="archive_videos")
    op.drop_table("archive_videos")

    op.drop_table("archive_modules")
