"""Add unique constraint on (olt_device_id, external_id) for OntUnit.

P1 FIX: Prevents duplicate ONT records when concurrent discovery
processes try to create the same ONT simultaneously.

Revision ID: 006_ont_external_id_unique
Revises: 005_add_olt_ping_fields
Create Date: 2026-04-01
"""

from sqlalchemy import text

from alembic import op

# revision identifiers
revision = "006_ont_external_id_unique"
down_revision = "005_add_olt_ping_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add unique constraint on (olt_device_id, external_id) for ont_units.

    This constraint is partial - only applies where both fields are NOT NULL.
    This allows ONTs without an OLT assignment or external_id to exist,
    while preventing duplicates for ONTs discovered from an OLT.
    """
    # Check if index already exists (idempotent)
    conn = op.get_bind()
    result = conn.execute(
        text(
            """
            SELECT 1 FROM pg_indexes
            WHERE indexname = 'uq_ont_units_olt_external_id'
            """
        )
    )
    if result.fetchone():
        return

    # Create partial unique index (more flexible than constraint)
    # Only enforces uniqueness where both olt_device_id and external_id are NOT NULL
    # Note: Cannot use CONCURRENTLY within a transaction, using regular CREATE INDEX
    op.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            uq_ont_units_olt_external_id
            ON ont_units (olt_device_id, external_id)
            WHERE olt_device_id IS NOT NULL AND external_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    """Remove the unique constraint."""
    op.execute(text("DROP INDEX IF EXISTS uq_ont_units_olt_external_id"))
