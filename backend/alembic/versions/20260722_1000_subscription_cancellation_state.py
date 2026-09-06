"""Track how an autorenew cancellation was actually dispatched.

Revision ID: 0022_subscription_cancel_state
Revises: 0021_admin_token_version

GK-377: ``cancel_at_period_end`` alone cannot say *why* it is set. It is true
both when the provider confirmed the cancellation and when we only wrote down
the user's request — and those two are operationally opposite: the first stops
a card charge, the second means somebody still has to go stop it by hand.

Additive columns, plus one **necessary** data fix.

The old handler set ``cancel_at_period_end = true`` on every cancellation
request without calling anyone, so the flag's historical meaning is ambiguous:
it may record a genuine provider confirmation (webhook echo) or the local-only
write that stranded the 2026-07 curator requests. The new code reads that flag
as proof of provider confirmation, so shipping without a backfill would render
those stranded rows as "Списаний больше не будет" — reintroducing the exact
false claim this task removes.

Ambiguous rows are therefore demoted to ``manual_required`` so a human verifies
them. Rows whose ``provider_status`` is already cancelled are unambiguous
(only a provider webhook writes that) and are left as provider-confirmed.
Over-flagging is the safe direction: a spurious queue item costs an admin one
check, while a missed one means a member is lied to and charged again.
"""
import sqlalchemy as sa

from alembic import op

revision = "0022_subscription_cancel_state"
down_revision = "0021_admin_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Kept as a plain string (not a Postgres ENUM) to match the existing
    # provider/provider_status columns and avoid ENUM churn on every new state.
    op.add_column(
        "subscriptions",
        sa.Column("cancel_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("cancel_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("cancel_failure_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("cancel_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "cancel_resolved_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
    )
    # The manual queue is "cancel_state = manual_required AND cancel_resolved_at
    # IS NULL" — indexed because the admin panel polls it.
    op.create_index("ix_sub_cancel_state", "subscriptions", ["cancel_state"])

    # Demote every ambiguous historical flag to the manual queue (see docstring).
    # cancel_requested_at is unknown for these; started_at is the honest stand-in
    # for ordering and is never NULL.
    op.execute(
        sa.text(
            """
            UPDATE subscriptions
               SET cancel_state = 'manual_required',
                   cancel_at_period_end = false,
                   cancel_requested_at = COALESCE(cancel_requested_at, started_at),
                   cancel_failure_reason =
                       'Backfilled by GK-377: this cancellation predates provider '
                       'dispatch and was never confirmed with the provider. Verify '
                       'in the provider dashboard before treating it as cancelled.'
             WHERE cancel_at_period_end = true
               AND (provider_status IS NULL
                    OR lower(provider_status) NOT IN ('cancelled', 'canceled'))
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_sub_cancel_state", table_name="subscriptions")
    op.drop_column("subscriptions", "cancel_resolved_by_admin_id")
    op.drop_column("subscriptions", "cancel_resolved_at")
    op.drop_column("subscriptions", "cancel_failure_reason")
    op.drop_column("subscriptions", "cancel_confirmed_at")
    op.drop_column("subscriptions", "cancel_requested_at")
    op.drop_column("subscriptions", "cancel_state")
