"""Add provisioning profile FKs to ont_units and catalog_offers, extend step type enum.

Revision ID: f4g7b9c1d3e5
Revises: e3f6a8b0c2d4
Create Date: 2026-03-10
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "f4g7b9c1d3e5"
down_revision = "e3f6a8b0c2d4"
branch_labels = None
depends_on = None


def _add_enum_value_if_not_exists(enum_name: str, value: str) -> None:
    """Add a value to an existing PostgreSQL enum type if not present."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_enum WHERE enumtypid = "
            "(SELECT oid FROM pg_type WHERE typname = :name) "
            "AND enumlabel = :val"
        ),
        {"name": enum_name, "val": value},
    )
    if result.fetchone() is None:
        # Cannot use parameterized DDL for ALTER TYPE
        conn.execute(
            sa.text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")
        )


def _column_exists(table: str, column: str) -> bool:
    """Check if a column exists on a table."""
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def upgrade() -> None:
    # Extend ProvisioningStepType enum with new values
    _add_enum_value_if_not_exists("provisioningsteptype", "resolve_profile")
    _add_enum_value_if_not_exists("provisioningsteptype", "push_ont_profile")
    _add_enum_value_if_not_exists("provisioningsteptype", "verify_ont_config")

    # Add provisioning profile columns to ont_units
    if not _column_exists("ont_units", "provisioning_profile_id"):
        op.add_column(
            "ont_units",
            sa.Column(
                "provisioning_profile_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ont_provisioning_profiles.id"),
                nullable=True,
            ),
        )
    if not _column_exists("ont_units", "provisioning_status"):
        op.add_column(
            "ont_units",
            sa.Column(
                "provisioning_status",
                sa.Enum(
                    "unprovisioned",
                    "provisioned",
                    "drift_detected",
                    "failed",
                    name="ontprovisioningstatus",
                    create_constraint=False,
                    create_type=False,
                ),
                nullable=True,
            ),
        )
    if not _column_exists("ont_units", "last_provisioned_at"):
        op.add_column(
            "ont_units",
            sa.Column("last_provisioned_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Add default ONT profile FK to catalog_offers
    if not _column_exists("catalog_offers", "default_ont_profile_id"):
        op.add_column(
            "catalog_offers",
            sa.Column(
                "default_ont_profile_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ont_provisioning_profiles.id"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("catalog_offers", "default_ont_profile_id")
    op.drop_column("ont_units", "last_provisioned_at")
    op.drop_column("ont_units", "provisioning_status")
    op.drop_column("ont_units", "provisioning_profile_id")
    # Note: Cannot remove enum values in PostgreSQL
