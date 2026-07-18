"""Add reviewed forwarding declarations and control-plane observations.

Revision ID: 362_forwarding_topology_declarations
Revises: 361_splitter_cascade_links
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "362_forwarding_topology_declarations"
down_revision = "361_splitter_cascade_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forwarding_topology_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("path_key", sa.String(120), nullable=False),
        sa.Column("declaration_payload", sa.JSON(), nullable=False),
        sa.Column("existing_declaration_id", postgresql.UUID(as_uuid=True)),
        sa.Column("expected_topology_sha256", sa.String(64), nullable=False),
        sa.Column("expected_declaration_sha256", sa.String(64)),
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
        sa.Column("result_declaration_id", postgresql.UUID(as_uuid=True)),
        sa.Column("result_payload", sa.JSON()),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('declare', 'retire')",
            name="ck_forwarding_topology_decision_action",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_forwarding_topology_decision_status",
        ),
        sa.CheckConstraint(
            "length(decision_sha256) = 64 "
            "AND length(expected_topology_sha256) = 64 "
            "AND (expected_declaration_sha256 IS NULL "
            "OR length(expected_declaration_sha256) = 64) "
            "AND (result_sha256 IS NULL OR length(result_sha256) = 64)",
            name="ck_forwarding_topology_decision_hashes",
        ),
        sa.CheckConstraint(
            "(action = 'declare' AND existing_declaration_id IS NULL "
            "AND expected_declaration_sha256 IS NULL) OR "
            "(action = 'retire' AND existing_declaration_id IS NOT NULL "
            "AND expected_declaration_sha256 IS NOT NULL)",
            name="ck_forwarding_topology_decision_existing",
        ),
        sa.CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_forwarding_topology_decision_review_separation",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status <> 'proposed' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_forwarding_topology_decision_review_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NULL AND executed_at IS NULL "
            "AND result_sha256 IS NULL AND result_declaration_id IS NULL) OR "
            "(status IN ('applied', 'closed') "
            "AND executed_by IS NOT NULL AND executed_at IS NOT NULL "
            "AND result_sha256 IS NOT NULL)",
            name="ck_forwarding_topology_decision_result_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('proposed', 'approved', 'applied') "
            "AND closed_reason IS NULL) OR "
            "(status IN ('declined', 'closed') AND closed_reason IS NOT NULL)",
            name="ck_forwarding_topology_decision_closed_reason",
        ),
        sa.CheckConstraint(
            "status <> 'applied' OR result_declaration_id IS NOT NULL",
            name="ck_forwarding_topology_decision_applied_declaration",
        ),
        sa.UniqueConstraint(
            "decision_sha256", name="uq_forwarding_topology_decision_sha256"
        ),
    )
    op.create_index(
        "ix_forwarding_topology_decision_path",
        "forwarding_topology_decisions",
        ["path_key", "status"],
    )
    op.create_index(
        "uq_forwarding_topology_active_decision_path",
        "forwarding_topology_decisions",
        ["path_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('proposed', 'approved')"),
        sqlite_where=sa.text("status IN ('proposed', 'approved')"),
    )

    op.create_table(
        "forwarding_topology_declarations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("path_key", sa.String(120), nullable=False),
        sa.Column("path_kind", sa.String(30), nullable=False),
        sa.Column(
            "downstream_device_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "downstream_interface_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "downstream_pop_site_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("downstream_role", sa.String(30), nullable=False),
        sa.Column("upstream_device_id", postgresql.UUID(as_uuid=True)),
        sa.Column("upstream_interface_id", postgresql.UUID(as_uuid=True)),
        sa.Column("upstream_pop_site_id", postgresql.UUID(as_uuid=True)),
        sa.Column("upstream_role", sa.String(30)),
        sa.Column("vrf_name", sa.String(120), nullable=False),
        sa.Column("peer_ip", sa.String(64)),
        sa.Column("peer_asn", sa.BigInteger()),
        sa.Column("route_prefix", sa.String(80)),
        sa.Column("next_hop_ip", sa.String(64)),
        sa.Column("nas_device_id", postgresql.UUID(as_uuid=True)),
        sa.Column("preference", sa.Integer(), nullable=False),
        sa.Column("configuration_owner", sa.String(120), nullable=False),
        sa.Column("configuration_intent_ref", sa.String(255), nullable=False),
        sa.Column(
            "created_by_decision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("retired_by_decision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("declaration_sha256", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "path_kind IN ('internal', 'border_peer', 'nas_termination')",
            name="ck_forwarding_topology_declaration_kind",
        ),
        sa.CheckConstraint(
            "downstream_role IN "
            "('access', 'aggregation', 'distribution', 'core', 'border', 'nas') "
            "AND (upstream_role IS NULL OR upstream_role IN "
            "('access', 'aggregation', 'distribution', 'core', 'border', 'nas'))",
            name="ck_forwarding_topology_declaration_roles",
        ),
        sa.CheckConstraint(
            "preference > 0",
            name="ck_forwarding_topology_declaration_preference",
        ),
        sa.CheckConstraint(
            "length(declaration_sha256) = 64",
            name="ck_forwarding_topology_declaration_sha256",
        ),
        sa.CheckConstraint(
            "(active AND retired_by_decision_id IS NULL AND retired_at IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL "
            "AND retired_at IS NOT NULL)",
            name="ck_forwarding_topology_declaration_retirement",
        ),
        sa.CheckConstraint(
            "(path_kind = 'internal' "
            "AND upstream_device_id IS NOT NULL "
            "AND upstream_interface_id IS NOT NULL "
            "AND upstream_pop_site_id IS NOT NULL "
            "AND upstream_role IS NOT NULL "
            "AND peer_ip IS NULL AND peer_asn IS NULL "
            "AND route_prefix IS NULL AND next_hop_ip IS NULL "
            "AND nas_device_id IS NULL) OR "
            "(path_kind = 'border_peer' "
            "AND downstream_role = 'border' "
            "AND upstream_device_id IS NULL "
            "AND upstream_interface_id IS NULL "
            "AND upstream_pop_site_id IS NULL "
            "AND upstream_role IS NULL "
            "AND peer_ip IS NOT NULL AND peer_asn IS NOT NULL "
            "AND route_prefix IS NOT NULL AND next_hop_ip IS NOT NULL "
            "AND nas_device_id IS NULL) OR "
            "(path_kind = 'nas_termination' "
            "AND downstream_role = 'nas' "
            "AND upstream_device_id IS NOT NULL "
            "AND upstream_interface_id IS NOT NULL "
            "AND upstream_pop_site_id IS NOT NULL "
            "AND upstream_role IS NOT NULL "
            "AND peer_ip IS NULL AND peer_asn IS NULL "
            "AND route_prefix IS NOT NULL AND next_hop_ip IS NOT NULL "
            "AND nas_device_id IS NOT NULL)",
            name="ck_forwarding_topology_declaration_shape",
        ),
        sa.CheckConstraint(
            "upstream_device_id IS NULL OR downstream_device_id <> upstream_device_id",
            name="ck_forwarding_topology_declaration_distinct_devices",
        ),
        sa.ForeignKeyConstraint(
            ["downstream_device_id"], ["network_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["downstream_interface_id"],
            ["device_interfaces.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["downstream_pop_site_id"], ["pop_sites.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["upstream_device_id"], ["network_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["upstream_interface_id"],
            ["device_interfaces.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_pop_site_id"], ["pop_sites.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["nas_device_id"], ["nas_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_decision_id"],
            ["forwarding_topology_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_decision_id"],
            ["forwarding_topology_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "created_by_decision_id",
            name="uq_forwarding_topology_declaration_create_decision",
        ),
        sa.UniqueConstraint(
            "retired_by_decision_id",
            name="uq_forwarding_topology_declaration_retire_decision",
        ),
        sa.UniqueConstraint(
            "declaration_sha256",
            name="uq_forwarding_topology_declaration_sha256",
        ),
    )
    op.create_index(
        "uq_forwarding_topology_active_path_key",
        "forwarding_topology_declarations",
        ["path_key"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_index(
        "uq_forwarding_topology_active_preference",
        "forwarding_topology_declarations",
        ["downstream_device_id", "vrf_name", "preference"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_index(
        "ix_forwarding_topology_active_downstream",
        "forwarding_topology_declarations",
        ["downstream_device_id", "vrf_name", "active"],
    )
    op.create_index(
        "ix_forwarding_topology_active_upstream",
        "forwarding_topology_declarations",
        ["upstream_device_id", "vrf_name", "active"],
    )

    op.create_table(
        "forwarding_control_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("collector", sa.String(120), nullable=False),
        sa.Column("collector_run_id", sa.String(160), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("interface_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vrf_name", sa.String(120), nullable=False),
        sa.Column("peer_ip", sa.String(64)),
        sa.Column("peer_asn", sa.BigInteger()),
        sa.Column("route_prefix", sa.String(80)),
        sa.Column("next_hop_ip", sa.String(64)),
        sa.Column("source_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('bgp_peer', 'routing_table')",
            name="ck_forwarding_control_observation_source",
        ),
        sa.CheckConstraint(
            "length(observation_sha256) = 64 AND length(source_evidence_sha256) = 64",
            name="ck_forwarding_control_observation_hashes",
        ),
        sa.CheckConstraint(
            "expires_at > observed_at",
            name="ck_forwarding_control_observation_expiry",
        ),
        sa.CheckConstraint(
            "(source_type = 'bgp_peer' "
            "AND peer_ip IS NOT NULL AND peer_asn IS NOT NULL "
            "AND route_prefix IS NULL AND next_hop_ip IS NULL) OR "
            "(source_type = 'routing_table' "
            "AND peer_ip IS NULL AND peer_asn IS NULL "
            "AND route_prefix IS NOT NULL AND next_hop_ip IS NOT NULL)",
            name="ck_forwarding_control_observation_shape",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["network_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["interface_id"], ["device_interfaces.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "client_ref", name="uq_forwarding_control_observation_client_ref"
        ),
        sa.UniqueConstraint(
            "observation_sha256",
            name="uq_forwarding_control_observation_sha256",
        ),
    )
    op.create_index(
        "ix_forwarding_control_observation_lookup",
        "forwarding_control_observations",
        ["source_type", "device_id", "interface_id", "vrf_name", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_forwarding_control_observation_lookup",
        table_name="forwarding_control_observations",
    )
    op.drop_table("forwarding_control_observations")

    op.drop_index(
        "ix_forwarding_topology_active_upstream",
        table_name="forwarding_topology_declarations",
    )
    op.drop_index(
        "ix_forwarding_topology_active_downstream",
        table_name="forwarding_topology_declarations",
    )
    op.drop_index(
        "uq_forwarding_topology_active_preference",
        table_name="forwarding_topology_declarations",
    )
    op.drop_index(
        "uq_forwarding_topology_active_path_key",
        table_name="forwarding_topology_declarations",
    )
    op.drop_table("forwarding_topology_declarations")

    op.drop_index(
        "uq_forwarding_topology_active_decision_path",
        table_name="forwarding_topology_decisions",
    )
    op.drop_index(
        "ix_forwarding_topology_decision_path",
        table_name="forwarding_topology_decisions",
    )
    op.drop_table("forwarding_topology_decisions")
