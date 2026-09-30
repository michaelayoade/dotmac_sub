"""Add subscriber notification windows: a generic, declared consolidation facility.

One table, scoped by ``group``, backs every registered entry in
``app.services.notification_consolidation.CONSOLIDATION_GROUPS`` -- today
only ``service_restoration``. A future unrelated correlated-event set
registers its own group and reuses this table; it never needs a new one.

Revision ID: 634_subscriber_notification_windows
Revises: 633_retire_system_admin_main_reseller_membership
Create Date: 2026-09-30

Renumbered from 633 to 634 during integration: two independent worktrees
(this one and the main-reseller-customer-mail-copy-leak fix) both branched
from 632 and independently picked "633". This migration is unrelated to that
one's content, so the two are chained sequentially rather than merged as
alembic branches. This means the reseller-membership fix
(633_retire_system_admin_main_reseller_membership) must land first.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "634_subscriber_notification_windows"
down_revision = "633_retire_system_admin_main_reseller_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriber_notification_windows",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("group", sa.String(length=80), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=20), nullable=True),
        sa.Column(
            "collected_events",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "close_reason IN ('completed', 'timeout')",
            name="ck_subscriber_notification_windows_close_reason",
        ),
    )
    # Access pattern 1: find the open window for a given subscriber in a given
    # consolidation group. A partial unique index also gives the DB itself a
    # second, independent guarantee (beyond application logic) that a
    # subscriber never has two concurrently open windows IN THE SAME GROUP --
    # scoped by group, not just subscriber_id, so independent groups (a future
    # one alongside today's sole "service_restoration") never block each
    # other for the same subscriber.
    op.create_index(
        "uq_subscriber_notification_windows_open_subscriber_group",
        "subscriber_notification_windows",
        ["subscriber_id", "group"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    # Access pattern 2: the sweep query — every window whose close deadline
    # has passed and that has not yet been closed.
    op.create_index(
        "ix_subscriber_notification_windows_sweep",
        "subscriber_notification_windows",
        ["window_closes_at"],
        unique=False,
        postgresql_where=sa.text("closed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscriber_notification_windows_sweep",
        table_name="subscriber_notification_windows",
    )
    op.drop_index(
        "uq_subscriber_notification_windows_open_subscriber_group",
        table_name="subscriber_notification_windows",
    )
    op.drop_table("subscriber_notification_windows")
