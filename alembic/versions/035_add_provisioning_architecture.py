"""Add OLT/ONT provisioning architecture improvements

Revision ID: 035_add_provisioning_architecture
Revises: 034_add_compensation_failures
Create Date: 2026-04-18

Phase 1: Service-port allocator (DB-backed index pool)
Phase 2: Async verification (verification_status, timestamps)
Phase 4: Circuit breaker (circuit_state, backoff, queued operations)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "035_add_provisioning_architecture"
down_revision = "034_add_compensation_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    # =========================================================================
    # Phase 1: Service-Port Allocator
    # =========================================================================

    # Create olt_service_port_pools table
    if "olt_service_port_pools" not in existing_tables:
        op.create_table(
            "olt_service_port_pools",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("olt_device_id", sa.UUID(), nullable=False),
            sa.Column("min_index", sa.Integer(), nullable=False, default=0),
            sa.Column("max_index", sa.Integer(), nullable=False, default=65535),
            sa.Column("reserved_indices", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("next_available_index", sa.Integer(), nullable=True),
            sa.Column("available_count", sa.Integer(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["olt_device_id"],
                ["olt_devices.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("olt_device_id", name="uq_olt_service_port_pools_olt"),
        )

    # Create service_port_allocations table
    if "service_port_allocations" not in existing_tables:
        op.create_table(
            "service_port_allocations",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("pool_id", sa.UUID(), nullable=False),
            sa.Column("ont_unit_id", sa.UUID(), nullable=False),
            sa.Column("port_index", sa.Integer(), nullable=False),
            sa.Column("vlan_id", sa.Integer(), nullable=True),
            sa.Column("gem_index", sa.Integer(), nullable=True),
            sa.Column("service_type", sa.String(length=40), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
            sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["pool_id"],
                ["olt_service_port_pools.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["ont_unit_id"],
                ["ont_units.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "pool_id", "port_index", name="uq_service_port_allocations_pool_index"
            ),
        )
        op.create_index(
            "ix_service_port_allocations_ont",
            "service_port_allocations",
            ["ont_unit_id"],
        )
        op.create_index(
            "ix_service_port_allocations_active",
            "service_port_allocations",
            ["is_active"],
        )

    # =========================================================================
    # Phase 2: Async Verification (add columns to ont_units)
    # =========================================================================

    ont_columns = {c["name"] for c in inspector.get_columns("ont_units")}

    if "last_applied_at" not in ont_columns:
        op.add_column(
            "ont_units",
            sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "last_verified_at" not in ont_columns:
        op.add_column(
            "ont_units",
            sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "verification_status" not in ont_columns:
        op.add_column(
            "ont_units",
            sa.Column("verification_status", sa.String(length=20), nullable=True),
        )

    # =========================================================================
    # Phase 4: Circuit Breaker (add columns to olt_devices)
    # =========================================================================

    olt_columns = {c["name"] for c in inspector.get_columns("olt_devices")}

    if "circuit_state" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column("circuit_state", sa.String(length=20), nullable=True),
        )

    if "circuit_failure_count" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "circuit_failure_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )

    if "backoff_until" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        )

    if "last_successful_ssh_at" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column("last_successful_ssh_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "circuit_failure_threshold" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "circuit_failure_threshold",
                sa.Integer(),
                nullable=False,
                server_default="3",
            ),
        )

    # Create queued_olt_operations table
    if "queued_olt_operations" not in existing_tables:
        op.create_table(
            "queued_olt_operations",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("olt_device_id", sa.UUID(), nullable=False),
            sa.Column("operation_type", sa.String(length=64), nullable=False),
            sa.Column("payload", postgresql.JSON(astext_type=sa.Text()), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["olt_device_id"],
                ["olt_devices.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
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


def downgrade() -> None:
    # Drop queued_olt_operations table
    op.drop_index("ix_queued_olt_operations_scheduled", table_name="queued_olt_operations")
    op.drop_index("ix_queued_olt_operations_olt_status", table_name="queued_olt_operations")
    op.drop_table("queued_olt_operations")

    # Remove circuit breaker columns from olt_devices
    op.drop_column("olt_devices", "circuit_failure_threshold")
    op.drop_column("olt_devices", "last_successful_ssh_at")
    op.drop_column("olt_devices", "backoff_until")
    op.drop_column("olt_devices", "circuit_failure_count")
    op.drop_column("olt_devices", "circuit_state")

    # Remove verification columns from ont_units
    op.drop_column("ont_units", "verification_status")
    op.drop_column("ont_units", "last_verified_at")
    op.drop_column("ont_units", "last_applied_at")

    # Drop service_port_allocations table
    op.drop_index("ix_service_port_allocations_active", table_name="service_port_allocations")
    op.drop_index("ix_service_port_allocations_ont", table_name="service_port_allocations")
    op.drop_table("service_port_allocations")

    # Drop olt_service_port_pools table
    op.drop_table("olt_service_port_pools")
