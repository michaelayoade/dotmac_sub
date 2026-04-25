"""Simplify OntBundleAssignmentStatus enum to only used values

Revision ID: 063_simplify_bundle_assignment_status
Revises: 062_add_olt_gem_indices
Create Date: 2026-04-24

The OntBundleAssignmentStatus enum had 7 values but only 2 were actually used:
- applied: Active assignment driving ONT config
- superseded: Replaced by a newer assignment

This migration:
1. Migrates any rows with unused statuses (draft, planned, applying, drifted, failed) to 'applied'
2. Creates a new enum with only the 2 used values
3. Swaps the column to use the new enum
4. Drops the old enum
"""

import sqlalchemy as sa

from alembic import op

revision = "063_simplify_bundle_assignment_status"
down_revision = "062_add_olt_gem_indices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Step 1: Migrate any rows with removed statuses to 'applied'
    # These statuses were never used in practice but may exist from tests or edge cases
    conn.execute(
        sa.text("""
            UPDATE ont_bundle_assignments
            SET status = 'applied'
            WHERE status IN ('draft', 'planned', 'applying', 'drifted', 'failed')
        """)
    )

    # Step 2: Create new enum with only the used values
    conn.execute(
        sa.text(
            "CREATE TYPE ontbundleassignmentstatus_v2 AS ENUM ('applied', 'superseded')"
        )
    )

    # Step 3: Drop the default before changing the type (PostgreSQL can't auto-cast defaults)
    conn.execute(
        sa.text("""
            ALTER TABLE ont_bundle_assignments
            ALTER COLUMN status DROP DEFAULT
        """)
    )

    # Step 4: Alter the column to use the new enum type
    # PostgreSQL requires casting through text
    conn.execute(
        sa.text("""
            ALTER TABLE ont_bundle_assignments
            ALTER COLUMN status TYPE ontbundleassignmentstatus_v2
            USING status::text::ontbundleassignmentstatus_v2
        """)
    )

    # Step 5: Set the new default
    conn.execute(
        sa.text("""
            ALTER TABLE ont_bundle_assignments
            ALTER COLUMN status SET DEFAULT 'applied'::ontbundleassignmentstatus_v2
        """)
    )

    # Step 6: Drop the old enum and rename the new one
    conn.execute(sa.text("DROP TYPE ontbundleassignmentstatus"))
    conn.execute(
        sa.text(
            "ALTER TYPE ontbundleassignmentstatus_v2 RENAME TO ontbundleassignmentstatus"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Step 1: Create old enum with all values
    conn.execute(
        sa.text("""
            CREATE TYPE ontbundleassignmentstatus_v2 AS ENUM (
                'draft', 'planned', 'applying', 'applied', 'drifted', 'failed', 'superseded'
            )
        """)
    )

    # Step 2: Alter the column to use the old enum type
    conn.execute(
        sa.text("""
            ALTER TABLE ont_bundle_assignments
            ALTER COLUMN status TYPE ontbundleassignmentstatus_v2
            USING status::text::ontbundleassignmentstatus_v2
        """)
    )

    # Step 3: Restore the old default
    conn.execute(
        sa.text("""
            ALTER TABLE ont_bundle_assignments
            ALTER COLUMN status SET DEFAULT 'draft'::ontbundleassignmentstatus_v2
        """)
    )

    # Step 4: Drop the new enum and rename the old one back
    conn.execute(sa.text("DROP TYPE ontbundleassignmentstatus"))
    conn.execute(
        sa.text(
            "ALTER TYPE ontbundleassignmentstatus_v2 RENAME TO ontbundleassignmentstatus"
        )
    )
