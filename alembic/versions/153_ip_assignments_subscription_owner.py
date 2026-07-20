"""Restore the legacy IP-assignment subscription-owner branch.

Revision ID: 153_ip_assignments_subscription_owner
Revises: 152_subscriber_additional_routes
Create Date: 2026-06-17

This revision was deployed and recorded by at least one production database
before the operation was re-parented as revision 158. Removing the revision
made that database impossible for Alembic to upgrade. Keep the original
operation idempotent; revision 329 merges this deployed branch back into the
canonical chain without stamping or rewriting migration history.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "153_ip_assignments_subscription_owner"
down_revision = "152_subscriber_additional_routes"
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
