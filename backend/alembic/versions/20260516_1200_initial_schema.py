"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-16 12:00:00

"""
import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tg_id", sa.BigInteger, nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("first_name", sa.String(128), nullable=True),
        sa.Column("last_name", sa.String(128), nullable=True),
        sa.Column("language", sa.String(8), nullable=False, server_default="ru"),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("referral_code", sa.String(16), nullable=False),
        sa.Column("referrer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_banned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("bonus_days", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_unique_constraint("uq_users_tg_id", "users", ["tg_id"])
    op.create_unique_constraint("uq_users_ref_code", "users", ["referral_code"])
    op.create_index("ix_users_tg_id", "users", ["tg_id"])
    op.create_index("ix_users_ref_code", "users", ["referral_code"])

    op.create_table(
        "plans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("price_rub", sa.Numeric(10, 2), nullable=False),
        sa.Column("price_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("duration_days", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )

    sub_status = sa.Enum("active", "expired", "cancelled", "gifted", name="sub_status")
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan_id", sa.Integer, sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("status", sub_status, nullable=False, server_default="active"),
        sa.Column("source", sa.String(32), nullable=False, server_default="stripe"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invite_link", sa.String(255), nullable=True),
        sa.Column("notified_expiring", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_sub_user", "subscriptions", ["user_id"])
    op.create_index("ix_sub_status", "subscriptions", ["status"])

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="admin"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    pay_status = sa.Enum("pending", "awaiting_review", "succeeded", "failed", "refunded", name="payment_status")
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan_id", sa.Integer, sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("status", pay_status, nullable=False, server_default="pending"),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("screenshot_url", sa.String(500), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("is_gift", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("gift_recipient_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Integer, sa.ForeignKey("admin_users.id"), nullable=True),
    )
    op.create_index("ix_pay_user", "payments", ["user_id"])
    op.create_index("ix_pay_provider", "payments", ["provider"])
    op.create_index("ix_pay_status", "payments", ["status"])
    op.create_index("ix_pay_external_id", "payments", ["external_id"])
    op.create_index("ix_pay_created_at", "payments", ["created_at"])

    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("referrer_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referee_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("bonus_days_granted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("first_payment_id", sa.Integer, sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_referee", "referrals", ["referee_id"])
    op.create_index("ix_referrals_referrer", "referrals", ["referrer_id"])

    op.create_table(
        "gifts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sender_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("receiver_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan_id", sa.Integer, sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("payment_id", sa.Integer, sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    bc_status = sa.Enum("draft", "sending", "sent", "failed", name="broadcast_status")
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("segment", sa.String(32), nullable=False, server_default="all"),
        sa.Column("status", bc_status, nullable=False, server_default="draft"),
        sa.Column("sent_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )

    msg_role = sa.Enum("user", "assistant", "system", name="msg_role")
    op.create_table(
        "support_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", msg_role, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sup_user", "support_messages", ["user_id"])
    op.create_index("ix_sup_created", "support_messages", ["created_at"])

    op.create_table(
        "integration_webhooks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("secret", sa.String(255), nullable=True),
        sa.Column("events", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_iw_provider", "integration_webhooks", ["provider"])

    op.create_table(
        "webhook_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("webhook_id", sa.Integer, sa.ForeignKey("integration_webhooks.id"), nullable=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wl_event", "webhook_log", ["event"])
    op.create_index("ix_wl_created", "webhook_log", ["created_at"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for t in (
        "settings",
        "webhook_log",
        "integration_webhooks",
        "support_messages",
        "broadcasts",
        "gifts",
        "referrals",
        "payments",
        "admin_users",
        "subscriptions",
        "plans",
        "users",
    ):
        op.drop_table(t)
    for enum_name in ("broadcast_status", "msg_role", "payment_status", "sub_status"):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
