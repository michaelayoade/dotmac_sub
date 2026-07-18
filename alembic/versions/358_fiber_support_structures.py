"""Add canonical fiber supports and reviewed exact mount edges.

Revision ID: 358_fiber_support_structures
Revises: 357_account_credit_deposit_lifecycle
Create Date: 2026-07-18
"""

from __future__ import annotations

import geoalchemy2
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "358_fiber_support_structures"
down_revision = "357_account_credit_deposit_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fiber_support_structures",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("support_type", sa.String(40), nullable=False),
        sa.Column("owner_name", sa.String(160)),
        sa.Column("ownership_status", sa.String(30), nullable=False),
        sa.Column("lifecycle_status", sa.String(30), nullable=False),
        sa.Column("inspection_status", sa.String(30), nullable=False),
        sa.Column("last_inspected_at", sa.DateTime(timezone=True)),
        sa.Column("next_inspection_due_at", sa.DateTime(timezone=True)),
        sa.Column("lease_status", sa.String(30), nullable=False),
        sa.Column("lease_reference", sa.String(160)),
        sa.Column("lease_starts_at", sa.DateTime(timezone=True)),
        sa.Column("lease_ends_at", sa.DateTime(timezone=True)),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="POINT", srid=4326, from_text="ST_GeomFromEWKT"
            ),
        ),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "support_type IN ('pole', 'tower', 'building_attachment', 'other')",
            name="ck_fiber_support_structures_type",
        ),
        sa.CheckConstraint(
            "ownership_status IN ('unknown', 'dotmac_owned', 'leased', 'third_party')",
            name="ck_fiber_support_structures_ownership",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('planned', 'active', 'suspended', 'retired')",
            name="ck_fiber_support_structures_lifecycle",
        ),
        sa.CheckConstraint(
            "inspection_status IN ('uninspected', 'due', 'passed', 'conditional', 'failed')",
            name="ck_fiber_support_structures_inspection",
        ),
        sa.CheckConstraint(
            "lease_status IN ('unknown', 'not_required', 'pending', 'active', 'expired', 'terminated')",
            name="ck_fiber_support_structures_lease",
        ),
        sa.UniqueConstraint("code", name="uq_fiber_support_structures_code"),
    )
    op.create_index(
        "ix_fiber_support_structures_lifecycle",
        "fiber_support_structures",
        ["lifecycle_status"],
    )
    op.create_index(
        "ix_fiber_support_structures_inspection",
        "fiber_support_structures",
        ["inspection_status"],
    )
    op.create_index(
        "ix_fiber_support_structures_lease",
        "fiber_support_structures",
        ["lease_status"],
    )

    op.create_table(
        "fiber_support_mount_decisions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column(
            "support_structure_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("mounted_asset_type", sa.String(40), nullable=False),
        sa.Column("mounted_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mount_role", sa.String(30), nullable=False),
        sa.Column("sequence", sa.Integer()),
        sa.Column("existing_mount_id", postgresql.UUID(as_uuid=True)),
        sa.Column("expected_support_state_sha256", sa.String(64), nullable=False),
        sa.Column("expected_asset_state_sha256", sa.String(64), nullable=False),
        sa.Column("expected_mount_state_sha256", sa.String(64)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(160)),
        sa.Column("review_notes", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("executed_by", sa.String(160)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("closed_reason", sa.String(160)),
        sa.Column("decision_sha256", sa.String(64), nullable=False),
        sa.Column("result_mount_id", postgresql.UUID(as_uuid=True)),
        sa.Column("result_payload", sa.JSON()),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('attach', 'detach')",
            name="ck_fiber_support_mount_decisions_action",
        ),
        sa.CheckConstraint(
            "mounted_asset_type IN ('fdh_cabinet', 'fiber_access_point', 'splice_closure', 'fiber_segment')",
            name="ck_fiber_support_mount_decisions_asset_type",
        ),
        sa.CheckConstraint(
            "mount_role IN ('hosted', 'route_support', 'anchor')",
            name="ck_fiber_support_mount_decisions_role",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_fiber_support_mount_decisions_status",
        ),
        sa.CheckConstraint(
            "(action = 'attach' AND existing_mount_id IS NULL) OR "
            "(action = 'detach' AND existing_mount_id IS NOT NULL)",
            name="ck_fiber_support_mount_decisions_existing_mount",
        ),
        sa.CheckConstraint(
            "(mounted_asset_type = 'fiber_segment' "
            "AND mount_role IN ('route_support', 'anchor') "
            "AND sequence IS NOT NULL AND sequence > 0) OR "
            "(mounted_asset_type <> 'fiber_segment' "
            "AND mount_role = 'hosted' AND sequence IS NULL)",
            name="ck_fiber_support_mount_decisions_shape",
        ),
        sa.CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_support_mount_decisions_review_separation",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_fiber_support_mount_decisions_review_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_fiber_support_mount_decisions_result_evidence",
        ),
        sa.CheckConstraint(
            "(status = 'applied' AND result_mount_id IS NOT NULL) OR "
            "status <> 'applied'",
            name="ck_fiber_support_mount_decisions_applied_mount",
        ),
        sa.CheckConstraint(
            "length(decision_sha256) = 64 AND "
            "length(expected_support_state_sha256) = 64 AND "
            "length(expected_asset_state_sha256) = 64 AND "
            "(expected_mount_state_sha256 IS NULL OR "
            "length(expected_mount_state_sha256) = 64) AND "
            "(result_sha256 IS NULL OR length(result_sha256) = 64)",
            name="ck_fiber_support_mount_decisions_digests",
        ),
        sa.ForeignKeyConstraint(
            ["support_structure_id"],
            ["fiber_support_structures.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "decision_sha256", name="uq_fiber_support_mount_decisions_digest"
        ),
    )
    op.create_index(
        "ix_fiber_support_mount_decisions_status",
        "fiber_support_mount_decisions",
        ["status"],
    )
    op.create_index(
        "uq_fiber_support_mount_decisions_active_asset",
        "fiber_support_mount_decisions",
        ["mounted_asset_type", "mounted_asset_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('proposed', 'approved')"),
        sqlite_where=sa.text("status IN ('proposed', 'approved')"),
    )
    op.create_index(
        "ix_fiber_support_mount_decisions_asset",
        "fiber_support_mount_decisions",
        ["mounted_asset_type", "mounted_asset_id"],
    )
    op.create_index(
        "ix_fiber_support_mount_decisions_support",
        "fiber_support_mount_decisions",
        ["support_structure_id"],
    )

    op.create_table(
        "fiber_support_mounts",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "support_structure_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("mounted_asset_type", sa.String(40), nullable=False),
        sa.Column("mounted_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mount_role", sa.String(30), nullable=False),
        sa.Column("sequence", sa.Integer()),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("installed_by", sa.String(160), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_by", sa.String(160)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mounted_asset_type IN ('fdh_cabinet', 'fiber_access_point', 'splice_closure', 'fiber_segment')",
            name="ck_fiber_support_mounts_asset_type",
        ),
        sa.CheckConstraint(
            "mount_role IN ('hosted', 'route_support', 'anchor')",
            name="ck_fiber_support_mounts_role",
        ),
        sa.CheckConstraint(
            "(mounted_asset_type = 'fiber_segment' "
            "AND mount_role IN ('route_support', 'anchor') "
            "AND sequence IS NOT NULL AND sequence > 0) OR "
            "(mounted_asset_type <> 'fiber_segment' "
            "AND mount_role = 'hosted' AND sequence IS NULL)",
            name="ck_fiber_support_mounts_shape",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["fiber_support_mount_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["support_structure_id"],
            ["fiber_support_structures.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("decision_id", name="uq_fiber_support_mounts_decision"),
    )
    op.create_index(
        "uq_fiber_support_mounts_active_edge",
        "fiber_support_mounts",
        ["support_structure_id", "mounted_asset_type", "mounted_asset_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )
    op.create_index(
        "uq_fiber_support_mounts_active_point_asset",
        "fiber_support_mounts",
        ["mounted_asset_type", "mounted_asset_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND mounted_asset_type <> 'fiber_segment'"),
        sqlite_where=sa.text("is_active = 1 AND mounted_asset_type <> 'fiber_segment'"),
    )
    op.create_index(
        "uq_fiber_support_mounts_active_segment_sequence",
        "fiber_support_mounts",
        ["mounted_asset_id", "sequence"],
        unique=True,
        postgresql_where=sa.text("is_active AND mounted_asset_type = 'fiber_segment'"),
        sqlite_where=sa.text("is_active = 1 AND mounted_asset_type = 'fiber_segment'"),
    )
    op.create_index(
        "ix_fiber_support_mounts_support",
        "fiber_support_mounts",
        ["support_structure_id"],
    )
    op.create_index(
        "ix_fiber_support_mounts_asset",
        "fiber_support_mounts",
        ["mounted_asset_type", "mounted_asset_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_fiber_support_mounts_asset", table_name="fiber_support_mounts")
    op.drop_index("ix_fiber_support_mounts_support", table_name="fiber_support_mounts")
    op.drop_index(
        "uq_fiber_support_mounts_active_segment_sequence",
        table_name="fiber_support_mounts",
    )
    op.drop_index(
        "uq_fiber_support_mounts_active_point_asset",
        table_name="fiber_support_mounts",
    )
    op.drop_index(
        "uq_fiber_support_mounts_active_edge", table_name="fiber_support_mounts"
    )
    op.drop_table("fiber_support_mounts")
    op.drop_index(
        "ix_fiber_support_mount_decisions_support",
        table_name="fiber_support_mount_decisions",
    )
    op.drop_index(
        "ix_fiber_support_mount_decisions_asset",
        table_name="fiber_support_mount_decisions",
    )
    op.drop_index(
        "uq_fiber_support_mount_decisions_active_asset",
        table_name="fiber_support_mount_decisions",
    )
    op.drop_index(
        "ix_fiber_support_mount_decisions_status",
        table_name="fiber_support_mount_decisions",
    )
    op.drop_table("fiber_support_mount_decisions")
    op.drop_index(
        "ix_fiber_support_structures_lease", table_name="fiber_support_structures"
    )
    op.drop_index(
        "ix_fiber_support_structures_inspection",
        table_name="fiber_support_structures",
    )
    op.drop_index(
        "ix_fiber_support_structures_lifecycle",
        table_name="fiber_support_structures",
    )
    op.drop_table("fiber_support_structures")
