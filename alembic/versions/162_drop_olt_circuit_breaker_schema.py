"""Drop the inert OLT SSH circuit-breaker schema.

The OLT SSH circuit-breaker + deferred-operations queue was removed (it was
never wired — the queue had no producers and the real write paths bypassed the
breaker). The code was deleted earlier; this drops the now-orphaned schema:

  * table ``queued_olt_operations`` (+ its two indexes)
  * ``OLTDevice`` columns ``circuit_state`` / ``circuit_failure_count`` /
    ``circuit_failure_threshold`` / ``backoff_until``

``OLTDevice.last_successful_ssh_at`` is kept (SSH telemetry, not breaker state).

Revision ID: 162_drop_olt_circuit_breaker_schema
Revises: 161_ip_pool_utilization_snapshots
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "162_drop_olt_circuit_breaker_schema"
down_revision = "161_ip_pool_utilization_snapshots"
branch_labels = None
depends_on = None

_BREAKER_COLUMNS = (
    "circuit_state",
    "circuit_failure_count",
    "circuit_failure_threshold",
    "backoff_until",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "queued_olt_operations" in inspector.get_table_names():
        op.drop_table("queued_olt_operations")

    existing = {c["name"] for c in inspector.get_columns("olt_devices")}
    drop = [c for c in _BREAKER_COLUMNS if c in existing]
    if drop:
        with op.batch_alter_table("olt_devices") as batch:
            for col in drop:
                batch.drop_column(col)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = {c["name"] for c in inspector.get_columns("olt_devices")}
    with op.batch_alter_table("olt_devices") as batch:
        if "circuit_state" not in existing:
            batch.add_column(sa.Column("circuit_state", sa.String(length=20)))
        if "circuit_failure_count" not in existing:
            batch.add_column(
                sa.Column(
                    "circuit_failure_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "backoff_until" not in existing:
            batch.add_column(sa.Column("backoff_until", sa.DateTime(timezone=True)))
        if "circuit_failure_threshold" not in existing:
            batch.add_column(
                sa.Column(
                    "circuit_failure_threshold",
                    sa.Integer(),
                    nullable=False,
                    server_default="3",
                )
            )

    if "queued_olt_operations" not in inspector.get_table_names():
        op.create_table(
            "queued_olt_operations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "olt_device_id",
                UUID(as_uuid=True),
                sa.ForeignKey("olt_devices.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("operation_type", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("scheduled_for", sa.DateTime(timezone=True)),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text()),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_queued_olt_operations_olt_status",
            "queued_olt_operations",
            ["olt_device_id", "status"],
        )
        op.create_index(
            "ix_queued_olt_operations_scheduled",
            "queued_olt_operations",
            ["scheduled_for"],
        )
