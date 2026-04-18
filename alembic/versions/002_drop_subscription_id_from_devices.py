"""Drop subscription_id from device tables.

This migration removes subscription_id foreign keys from device tables as part of
decoupling OLT/device management from subscription management. Devices now link
directly to subscribers, enabling independent OLT management without requiring
subscription context.

Affected tables:
- cpe_devices
- ip_assignments
- ont_assignments
- splitter_port_assignments

Revision ID: 002_drop_subscription_id_from_devices
Revises: 001_squashed_initial_schema
Create Date: 2026-04-01

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "002_drop_subscription_id_from_devices"
down_revision = "001_squashed"
branch_labels = None
depends_on = None


def _drop_constraint_if_exists(table_name: str, constraint_name: str) -> None:
    """Drop a foreign key constraint when upgrading older schemas."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {fk["name"] for fk in inspector.get_foreign_keys(table_name)}
    if constraint_name in existing:
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    if column_name in existing:
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    """Remove subscription_id from device tables."""
    # Drop foreign key constraints first, then columns

    # cpe_devices
    _drop_constraint_if_exists(
        "cpe_devices",
        "cpe_devices_subscription_id_fkey",
    )
    _drop_column_if_exists("cpe_devices", "subscription_id")

    # ip_assignments - has both subscription_id and subscription_add_on_id
    _drop_constraint_if_exists(
        "ip_assignments",
        "ip_assignments_subscription_id_fkey",
    )
    _drop_column_if_exists("ip_assignments", "subscription_id")

    # Check if subscription_add_on_id column exists before dropping
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("ip_assignments")]
    if "subscription_add_on_id" in columns:
        # Check for FK constraint
        fks = inspector.get_foreign_keys("ip_assignments")
        for fk in fks:
            if "subscription_add_on_id" in fk.get("constrained_columns", []):
                op.drop_constraint(fk["name"], "ip_assignments", type_="foreignkey")
        op.drop_column("ip_assignments", "subscription_add_on_id")

    # ont_assignments
    _drop_constraint_if_exists(
        "ont_assignments",
        "ont_assignments_subscription_id_fkey",
    )
    _drop_column_if_exists("ont_assignments", "subscription_id")

    # splitter_port_assignments
    _drop_constraint_if_exists(
        "splitter_port_assignments",
        "splitter_port_assignments_subscription_id_fkey",
    )
    _drop_column_if_exists("splitter_port_assignments", "subscription_id")


def downgrade() -> None:
    """Restore subscription_id columns to device tables.

    Note: This restores the columns but data will be lost. The columns will be
    nullable and empty after downgrade.
    """
    # splitter_port_assignments
    op.add_column(
        "splitter_port_assignments",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "splitter_port_assignments_subscription_id_fkey",
        "splitter_port_assignments",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )

    # ont_assignments
    op.add_column(
        "ont_assignments",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "ont_assignments_subscription_id_fkey",
        "ont_assignments",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )

    # ip_assignments
    op.add_column(
        "ip_assignments",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "ip_assignments_subscription_id_fkey",
        "ip_assignments",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )
    op.add_column(
        "ip_assignments",
        sa.Column(
            "subscription_add_on_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "ip_assignments_subscription_add_on_id_fkey",
        "ip_assignments",
        "subscription_add_ons",
        ["subscription_add_on_id"],
        ["id"],
    )

    # cpe_devices
    op.add_column(
        "cpe_devices",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "cpe_devices_subscription_id_fkey",
        "cpe_devices",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )
