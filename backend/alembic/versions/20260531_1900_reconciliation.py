"""reconciliation runs and discrepancy items

Revision ID: 0008_reconciliation
Revises: 0007_portal
Create Date: 2026-05-31 19:00:00

GK-070: daily provider reconciliation with admin resolution history.
Statuses stay plain VARCHAR with check constraints; no new Postgres ENUMs.
"""
import sqlalchemy as sa

from alembic import op

revision = "0008_reconciliation"
down_revision = "0007_portal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("triggered_by", sa.String(32), nullable=False, server_default="scheduler"),
        sa.Column("provider_scope", sa.String(32), nullable=False, server_default="all"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("open_items_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("summary", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("error", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_errors', 'failed')",
            name="ck_reconciliation_runs_status",
        ),
        sa.CheckConstraint(
            "triggered_by IN ('scheduler', 'admin', 'manual', 'test')",
            name="ck_reconciliation_runs_triggered_by",
        ),
    )
    op.create_index("ix_reconciliation_runs_status", "reconciliation_runs", ["status"])
    op.create_index(
        "ix_reconciliation_runs_triggered_by", "reconciliation_runs", ["triggered_by"]
    )
    op.create_index(
        "ix_reconciliation_runs_provider_scope",
        "reconciliation_runs",
        ["provider_scope"],
    )
    op.create_index("ix_reconciliation_runs_started_at", "reconciliation_runs", ["started_at"])

    op.create_table(
        "reconciliation_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer,
            sa.ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("issue_type", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("expected_state", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("observed_state", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("resolve_note", sa.Text, nullable=True),
        sa.Column(
            "resolved_by_admin_id",
            sa.Integer,
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_reconciliation_items_status",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_reconciliation_items_severity",
        ),
    )
    op.create_index("ix_reconciliation_items_run_id", "reconciliation_items", ["run_id"])
    op.create_index("ix_reconciliation_items_provider", "reconciliation_items", ["provider"])
    op.create_index("ix_reconciliation_items_severity", "reconciliation_items", ["severity"])
    op.create_index(
        "ix_reconciliation_items_issue_type", "reconciliation_items", ["issue_type"]
    )
    op.create_index(
        "ix_reconciliation_items_entity_type", "reconciliation_items", ["entity_type"]
    )
    op.create_index("ix_reconciliation_items_entity_id", "reconciliation_items", ["entity_id"])
    op.create_index(
        "ix_reconciliation_items_external_id", "reconciliation_items", ["external_id"]
    )
    op.create_index("ix_reconciliation_items_status", "reconciliation_items", ["status"])
    op.create_index(
        "ix_reconciliation_items_resolved_by_admin_id",
        "reconciliation_items",
        ["resolved_by_admin_id"],
    )
    op.create_index(
        "ix_reconciliation_items_created_at", "reconciliation_items", ["created_at"]
    )
    op.create_index(
        "ix_reconciliation_items_run_status",
        "reconciliation_items",
        ["run_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_items_run_status", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_created_at", table_name="reconciliation_items")
    op.drop_index(
        "ix_reconciliation_items_resolved_by_admin_id", table_name="reconciliation_items"
    )
    op.drop_index("ix_reconciliation_items_status", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_external_id", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_entity_id", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_entity_type", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_issue_type", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_severity", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_provider", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_run_id", table_name="reconciliation_items")
    op.drop_table("reconciliation_items")

    op.drop_index("ix_reconciliation_runs_started_at", table_name="reconciliation_runs")
    op.drop_index("ix_reconciliation_runs_provider_scope", table_name="reconciliation_runs")
    op.drop_index("ix_reconciliation_runs_triggered_by", table_name="reconciliation_runs")
    op.drop_index("ix_reconciliation_runs_status", table_name="reconciliation_runs")
    op.drop_table("reconciliation_runs")
