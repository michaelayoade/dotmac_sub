"""Enforce operational cable and splitter capacity integrity.

Revision ID: 361_fiber_plant_operational_integrity
Revises: 360_forwarding_topology_declarations
Create Date: 2026-07-18

The migration deliberately fails closed when active plant does not satisfy the
new invariant. It never invents endpoints, geometry, cable size, or splitter
size and it never silently deactivates inventory.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "361_fiber_plant_operational_integrity"
down_revision = "360_forwarding_topology_declarations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            invalid_segments bigint;
            invalid_splitters bigint;
            invalid_connectivity_decisions bigint;
        BEGIN
            SELECT count(*) INTO invalid_segments
            FROM fiber_segments
            WHERE is_active
              AND (
                  from_point_id IS NULL
                  OR to_point_id IS NULL
                  OR from_point_id = to_point_id
                  OR route_geom IS NULL
                  OR fiber_count IS NULL
                  OR fiber_count <= 0
              );

            SELECT count(*) INTO invalid_splitters
            FROM splitters
            WHERE is_active
              AND (
                  input_ports <= 0
                  OR output_ports <= 0
                  OR splitter_ratio IS NULL
                  OR splitter_ratio <> CAST(input_ports AS varchar) || ':' ||
                     CAST(output_ports AS varchar)
              );

            SELECT count(*) INTO invalid_connectivity_decisions
            FROM fiber_topology_connectivity_decisions
            WHERE action = 'create' AND fiber_count IS NULL;

            IF invalid_segments > 0 OR invalid_splitters > 0
               OR invalid_connectivity_decisions > 0 THEN
                RAISE EXCEPTION
                    'fiber plant integrity preflight failed: % active cable segment(s), % active splitter(s), % unsized create decision(s)',
                    invalid_segments, invalid_splitters,
                    invalid_connectivity_decisions;
            END IF;
        END
        $$;
        """
    )

    op.create_check_constraint(
        "ck_fiber_segments_active_operational_shape",
        "fiber_segments",
        "NOT is_active OR (from_point_id IS NOT NULL "
        "AND to_point_id IS NOT NULL AND from_point_id <> to_point_id "
        "AND route_geom IS NOT NULL AND fiber_count IS NOT NULL "
        "AND fiber_count > 0)",
    )
    op.create_check_constraint(
        "ck_splitters_active_declared_capacity",
        "splitters",
        "NOT is_active OR (input_ports > 0 AND output_ports > 0 "
        "AND splitter_ratio IS NOT NULL "
        "AND splitter_ratio = CAST(input_ports AS VARCHAR) || ':' || "
        "CAST(output_ports AS VARCHAR))",
    )
    op.create_check_constraint(
        "ck_fiber_connectivity_create_capacity",
        "fiber_topology_connectivity_decisions",
        "action <> 'create' OR fiber_count IS NOT NULL",
    )

    op.add_column(
        "fiber_strands",
        sa.Column(
            "segment_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Exact cable-segment identity; cable_name is display metadata only",
        ),
    )
    op.create_foreign_key(
        "fk_fiber_strands_segment",
        "fiber_strands",
        "fiber_segments",
        ["segment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        UPDATE fiber_strands AS strand
        SET segment_id = exact_link.segment_id
        FROM (
            SELECT fiber_strand_id, (array_agg(id ORDER BY id))[1] AS segment_id
            FROM fiber_segments
            WHERE fiber_strand_id IS NOT NULL
            GROUP BY fiber_strand_id
            HAVING count(*) = 1
        ) AS exact_link
        WHERE strand.id = exact_link.fiber_strand_id
          AND strand.segment_id IS NULL
        """
    )
    op.create_unique_constraint(
        "uq_fiber_strands_segment_strand",
        "fiber_strands",
        ["segment_id", "strand_number"],
    )
    op.create_index(
        "ix_fiber_strands_segment_status",
        "fiber_strands",
        ["segment_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_fiber_strands_segment_status", table_name="fiber_strands")
    op.drop_constraint(
        "uq_fiber_strands_segment_strand", "fiber_strands", type_="unique"
    )
    op.drop_constraint("fk_fiber_strands_segment", "fiber_strands", type_="foreignkey")
    op.drop_column("fiber_strands", "segment_id")
    op.drop_constraint(
        "ck_fiber_connectivity_create_capacity",
        "fiber_topology_connectivity_decisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_splitters_active_declared_capacity", "splitters", type_="check"
    )
    op.drop_constraint(
        "ck_fiber_segments_active_operational_shape",
        "fiber_segments",
        type_="check",
    )
