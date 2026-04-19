"""add saga_executions table

Revision ID: 037_add_saga_executions
Revises: 036_add_zabbix_host_id_columns
Create Date: 2026-04-19

Tracks saga execution history for ONT provisioning workflows.
Enables observability of multi-step provisioning with compensation.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "037_add_saga_executions"
down_revision = "036_add_zabbix_host_id_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if table already exists (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "saga_executions" in inspector.get_table_names():
        return

    # Create enum type if it doesn't exist
    sagaexecutionstatus = postgresql.ENUM(
        "pending",
        "running",
        "succeeded",
        "failed",
        "compensating",
        "compensation_failed",
        name="sagaexecutionstatus",
        create_type=False,
    )
    sagaexecutionstatus.create(conn, checkfirst=True)

    op.create_table(
        "saga_executions",
        # Primary key
        sa.Column("id", sa.UUID(), nullable=False),
        # Saga identification
        sa.Column("saga_name", sa.String(length=128), nullable=False),
        sa.Column(
            "saga_version", sa.String(length=32), nullable=False, server_default="1.0"
        ),
        # Target references
        sa.Column("ont_unit_id", sa.UUID(), nullable=True),
        sa.Column("olt_device_id", sa.UUID(), nullable=True),
        # Execution status
        sa.Column(
            "status",
            sagaexecutionstatus,
            nullable=False,
            server_default="pending",
        ),
        # Input/Output data
        sa.Column(
            "input_data",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("output_data", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        # Step tracking
        sa.Column(
            "steps_executed",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "steps_compensated",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "compensation_failures",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        # Failure details
        sa.Column("failed_step", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Timing
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        # Audit
        sa.Column("initiated_by", sa.String(length=128), nullable=True),
        sa.Column("correlation_key", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Foreign keys
        sa.ForeignKeyConstraint(
            ["ont_unit_id"],
            ["ont_units.id"],
        ),
        sa.ForeignKeyConstraint(
            ["olt_device_id"],
            ["olt_devices.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes
    op.create_index(
        "ix_saga_executions_ont_unit",
        "saga_executions",
        ["ont_unit_id"],
    )
    op.create_index(
        "ix_saga_executions_olt_device",
        "saga_executions",
        ["olt_device_id"],
    )
    op.create_index(
        "ix_saga_executions_status",
        "saga_executions",
        ["status"],
    )
    op.create_index(
        "ix_saga_executions_saga_name",
        "saga_executions",
        ["saga_name"],
    )
    op.create_index(
        "ix_saga_executions_started_at",
        "saga_executions",
        ["started_at"],
    )
    op.create_index(
        "ix_saga_executions_correlation_key",
        "saga_executions",
        ["correlation_key"],
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_saga_executions_correlation_key", table_name="saga_executions")
    op.drop_index("ix_saga_executions_started_at", table_name="saga_executions")
    op.drop_index("ix_saga_executions_saga_name", table_name="saga_executions")
    op.drop_index("ix_saga_executions_status", table_name="saga_executions")
    op.drop_index("ix_saga_executions_olt_device", table_name="saga_executions")
    op.drop_index("ix_saga_executions_ont_unit", table_name="saga_executions")

    # Drop table
    op.drop_table("saga_executions")

    # Note: We don't drop the enum type as it may be used elsewhere
