"""security hardening: audit_log table

Revision ID: 0002_audit_log
Revises: 0001_initial
Create Date: 2026-05-16 18:00:00

"""
import sqlalchemy as sa

from alembic import op

revision = "0002_audit_log"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "actor_admin_id",
            sa.Integer,
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
        sa.Column("actor_ip", sa.String(64), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_admin_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_created", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_created", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_table("audit_log")
