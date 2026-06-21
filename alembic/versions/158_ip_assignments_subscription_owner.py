"""Add subscription owner to IP assignments.

Revision ID: 158_ip_assignments_subscription_owner
Revises: 157_outage_incidents
Create Date: 2026-06-17

Re-parented onto 157_outage_incidents (the real applied chain). Originally
authored off 152_subscriber_additional_routes as a second 153_* head; the
prod DB advanced down the topology branch (153_topology_zabbix_linkage ->
157) and never recorded this branch, so the column was added out-of-band.
This migration is idempotent (guards on column/index existence).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "158_ip_assignments_subscription_owner"
down_revision = "157_outage_incidents"
branch_labels = None
depends_on = None

TABLE = "ip_assignments"
COLUMN = "subscription_id"


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_index(table: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table))


def upgrade() -> None:
    if not _has_column(TABLE, COLUMN):
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                UUID(as_uuid=True),
                sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if not _has_index(TABLE, "ix_ip_assignments_subscription_id"):
        op.create_index(
            "ix_ip_assignments_subscription_id",
            TABLE,
            [COLUMN],
            unique=False,
        )


def downgrade() -> None:
    if _has_index(TABLE, "ix_ip_assignments_subscription_id"):
        op.drop_index("ix_ip_assignments_subscription_id", table_name=TABLE)
    if _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)
