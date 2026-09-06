"""admin totp 2fa

Revision ID: 0010_admin_totp
Revises: 0009_archive_showcases
Create Date: 2026-06-01 09:00:00

GK-130: add opt-in TOTP fields to admin users. Purely additive so existing
admins continue to authenticate with password-only until they enroll.
"""
import sqlalchemy as sa

from alembic import op

revision = "0010_admin_totp"
down_revision = "0009_archive_showcases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("admin_users", sa.Column("totp_secret", sa.String(64), nullable=True))
    op.add_column(
        "admin_users",
        sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "admin_users",
        sa.Column("totp_disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("admin_users", sa.Column("totp_last_counter", sa.Integer(), nullable=True))
    op.add_column(
        "admin_users",
        sa.Column("totp_recovery_hashes", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("admin_users", "totp_recovery_hashes")
    op.drop_column("admin_users", "totp_last_counter")
    op.drop_column("admin_users", "totp_disabled_at")
    op.drop_column("admin_users", "totp_confirmed_at")
    op.drop_column("admin_users", "totp_secret")
    op.drop_column("admin_users", "totp_enabled")
