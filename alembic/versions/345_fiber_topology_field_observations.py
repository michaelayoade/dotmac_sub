"""Add immutable staged fiber field observations.

Revision ID: 345_fiber_topology_field_observations
Revises: 344_fiber_connectivity_batch_control
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "345_fiber_topology_field_observations"
down_revision = "344_fiber_connectivity_batch_control"
branch_labels = None
depends_on = None

_TABLE = "fiber_topology_field_observations"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("staged_feature_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_content_sha256", sa.String(64), nullable=False),
        sa.Column("source_system", sa.String(40), nullable=False),
        sa.Column("source_profile", sa.String(40), nullable=False),
        sa.Column("source_asset_type", sa.String(40), nullable=False),
        sa.Column("source_external_id", sa.String(255)),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_order_public_id", sa.String(64), nullable=False),
        sa.Column("verification_scope", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("observed_external_label", sa.String(255)),
        sa.Column("observed_asset_type", sa.String(40)),
        sa.Column("observed_asset_id", postgresql.UUID(as_uuid=True)),
        sa.Column("start_endpoint_type", sa.String(40)),
        sa.Column("start_endpoint_ref_id", postgresql.UUID(as_uuid=True)),
        sa.Column("end_endpoint_type", sa.String(40)),
        sa.Column("end_endpoint_ref_id", postgresql.UUID(as_uuid=True)),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("accuracy_m", sa.Float()),
        sa.Column("instrument", sa.String(120)),
        sa.Column("measurement_payload", sa.JSON(), nullable=False),
        sa.Column("attachment_ids", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("claim_sha256", sa.String(64), nullable=False),
        sa.Column("observation_sha256", sa.String(64), nullable=False),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recorded_by_technician_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "recorded_by_person_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("recorded_by_system_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verification_scope IN "
            "('identity', 'presence', 'start_endpoint', 'end_endpoint', "
            "'path_endpoints')",
            name="ck_fiber_topology_field_observation_scope",
        ),
        sa.CheckConstraint(
            "outcome IN "
            "('agrees', 'conflicts', 'not_found', 'inaccessible', 'inconclusive')",
            name="ck_fiber_topology_field_observation_outcome",
        ),
        sa.CheckConstraint(
            "length(feature_content_sha256) = 64 "
            "AND length(claim_sha256) = 64 "
            "AND length(observation_sha256) = 64",
            name="ck_fiber_topology_field_observation_hashes",
        ),
        sa.CheckConstraint(
            "(observed_asset_type IS NULL AND observed_asset_id IS NULL) OR "
            "(observed_asset_type IS NOT NULL AND observed_asset_id IS NOT NULL)",
            name="ck_fiber_topology_field_observation_asset_pair",
        ),
        sa.CheckConstraint(
            "(start_endpoint_type IS NULL AND start_endpoint_ref_id IS NULL) OR "
            "(start_endpoint_type IS NOT NULL AND start_endpoint_ref_id IS NOT NULL)",
            name="ck_fiber_topology_field_observation_start_pair",
        ),
        sa.CheckConstraint(
            "(end_endpoint_type IS NULL AND end_endpoint_ref_id IS NULL) OR "
            "(end_endpoint_type IS NOT NULL AND end_endpoint_ref_id IS NOT NULL)",
            name="ck_fiber_topology_field_observation_end_pair",
        ),
        sa.CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)",
            name="ck_fiber_topology_field_observation_coordinates",
        ),
        sa.CheckConstraint(
            "accuracy_m IS NULL OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND accuracy_m BETWEEN 0 AND 10000)",
            name="ck_fiber_topology_field_observation_accuracy",
        ),
        sa.ForeignKeyConstraint(
            ["staged_feature_id"],
            ["fiber_topology_staged_features.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_order_id"], ["work_order.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_technician_id"],
            ["technician_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_system_user_id"],
            ["system_users.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "observation_sha256",
            name="uq_fiber_topology_field_observation_sha256",
        ),
        sa.UniqueConstraint(
            "client_ref",
            name="uq_fiber_topology_field_observation_client_ref",
        ),
    )
    op.create_index(
        "ix_fiber_topology_field_observation_source",
        _TABLE,
        ["source_system", "source_asset_type", "source_external_id"],
    )
    op.create_index(
        "ix_fiber_topology_field_observation_feature_content",
        _TABLE,
        ["staged_feature_id", "feature_content_sha256"],
    )
    op.create_index(
        "ix_fiber_topology_field_observation_work_order",
        _TABLE,
        ["work_order_id", "observed_at"],
    )
    op.create_index(
        "ix_fiber_topology_field_observation_observed",
        _TABLE,
        ["observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_fiber_topology_field_observation_observed", table_name=_TABLE)
    op.drop_index("ix_fiber_topology_field_observation_work_order", table_name=_TABLE)
    op.drop_index(
        "ix_fiber_topology_field_observation_feature_content", table_name=_TABLE
    )
    op.drop_index("ix_fiber_topology_field_observation_source", table_name=_TABLE)
    op.drop_table(_TABLE)
