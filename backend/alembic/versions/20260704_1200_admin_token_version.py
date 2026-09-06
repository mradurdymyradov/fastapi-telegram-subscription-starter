"""Add AdminUser.token_version for session-epoch invalidation.

Revision ID: 0021_admin_token_version
Revises: 0020_ref_discount_reservation

GK-405: purely additive account epoch. Signed into the admin JWT (`ver` claim)
and compared in current_admin so a password/TOTP/disable change (or an explicit
"revoke all sessions") invalidates every previously issued token. Existing rows
default to 0; legacy tokens without a `ver` claim are treated as epoch 0.
"""
import sqlalchemy as sa

from alembic import op

revision = "0021_admin_token_version"
down_revision = "0020_ref_discount_reservation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("admin_users", "token_version")
