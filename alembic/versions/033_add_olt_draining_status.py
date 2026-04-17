"""Add 'draining' status to DeviceStatus enum.

The draining status blocks new ONT authorizations while preserving
existing service. This enables controlled OLT decommissioning.

Revision ID: 033_add_olt_draining_status
Revises: 032_add_task_executions
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op

revision = "033_add_olt_draining_status"
down_revision = "032_add_task_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add 'draining' value to devicestatus enum after 'maintenance'
    # Using IF NOT EXISTS for idempotency
    op.execute(
        "ALTER TYPE devicestatus ADD VALUE IF NOT EXISTS 'draining' AFTER 'maintenance'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly.
    # The safest approach is to leave the enum value in place.
    # If removal is truly needed, a full enum rebuild would be required,
    # but that's disruptive and generally not recommended for production.
    pass
