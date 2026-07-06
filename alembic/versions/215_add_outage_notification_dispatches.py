"""Add outage_notification_dispatches (persisted debounce + send audit).

Durable, cross-worker record for customer outage notifications (outage
classifier P4, design docs/designs/OUTAGE_CLASSIFIER.md §P4). Replaces the old
in-memory debounce dict: a boundary is muted while it has a recent ``sent`` row,
and every dispatch attempt is audited (who, when, which operator, outcome).

All columns are plain types (no Postgres ENUM) on purpose — status/scope/channel
are short strings so this migration never emits CREATE TYPE.

Revision ID: 215_add_outage_notification_dispatches
Revises: 214_add_ont_signal_observations
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "215_add_outage_notification_dispatches"
down_revision = "214_add_ont_signal_observations"
branch_labels = None
depends_on = None

_TABLE = "outage_notification_dispatches"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("boundary_node_id", UUID(as_uuid=True), nullable=True),
        sa.Column("subscriber_id", UUID(as_uuid=True), nullable=True),
        sa.Column("subscription_id", UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(40), nullable=True),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=True),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("dedup_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_outage_notif_dispatch_boundary",
        _TABLE,
        ["boundary_node_id", "status", "created_at"],
    )
    op.create_index(
        "ix_outage_notif_dispatch_dedup", _TABLE, ["dedup_key", "created_at"]
    )
    op.create_index(
        "ix_outage_notif_dispatch_subscriber",
        _TABLE,
        ["subscriber_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    op.drop_index("ix_outage_notif_dispatch_subscriber", table_name=_TABLE)
    op.drop_index("ix_outage_notif_dispatch_dedup", table_name=_TABLE)
    op.drop_index("ix_outage_notif_dispatch_boundary", table_name=_TABLE)
    op.drop_table(_TABLE)
