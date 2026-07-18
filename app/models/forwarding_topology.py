from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ForwardingTopologyDecision(Base):
    """Immutable reviewed decision to declare or retire one forwarding path."""

    __tablename__ = "forwarding_topology_decisions"
    __table_args__ = (
        UniqueConstraint(
            "decision_sha256", name="uq_forwarding_topology_decision_sha256"
        ),
        CheckConstraint(
            "action IN ('declare', 'retire')",
            name="ck_forwarding_topology_decision_action",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_forwarding_topology_decision_status",
        ),
        CheckConstraint(
            "length(decision_sha256) = 64 "
            "AND length(expected_topology_sha256) = 64 "
            "AND (expected_declaration_sha256 IS NULL "
            "OR length(expected_declaration_sha256) = 64) "
            "AND (result_sha256 IS NULL OR length(result_sha256) = 64)",
            name="ck_forwarding_topology_decision_hashes",
        ),
        CheckConstraint(
            "(action = 'declare' AND existing_declaration_id IS NULL "
            "AND expected_declaration_sha256 IS NULL) OR "
            "(action = 'retire' AND existing_declaration_id IS NOT NULL "
            "AND expected_declaration_sha256 IS NOT NULL)",
            name="ck_forwarding_topology_decision_existing",
        ),
        CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_forwarding_topology_decision_review_separation",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status <> 'proposed' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_forwarding_topology_decision_review_evidence",
        ),
        CheckConstraint(
            "(status IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NULL AND executed_at IS NULL "
            "AND result_sha256 IS NULL AND result_declaration_id IS NULL) OR "
            "(status IN ('applied', 'closed') "
            "AND executed_by IS NOT NULL AND executed_at IS NOT NULL "
            "AND result_sha256 IS NOT NULL)",
            name="ck_forwarding_topology_decision_result_evidence",
        ),
        CheckConstraint(
            "(status IN ('proposed', 'approved', 'applied') "
            "AND closed_reason IS NULL) OR "
            "(status IN ('declined', 'closed') AND closed_reason IS NOT NULL)",
            name="ck_forwarding_topology_decision_closed_reason",
        ),
        CheckConstraint(
            "status <> 'applied' OR result_declaration_id IS NOT NULL",
            name="ck_forwarding_topology_decision_applied_declaration",
        ),
        Index("ix_forwarding_topology_decision_path", "path_key", "status"),
        Index(
            "uq_forwarding_topology_active_decision_path",
            "path_key",
            unique=True,
            postgresql_where=text("status IN ('proposed', 'approved')"),
            sqlite_where=text("status IN ('proposed', 'approved')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    path_key: Mapped[str] = mapped_column(String(120), nullable=False)
    declaration_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    existing_declaration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    expected_topology_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_declaration_sha256: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_by: Mapped[str | None] = mapped_column(String(160))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    closed_reason: Mapped[str | None] = mapped_column(String(160))
    decision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_declaration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_payload: Mapped[dict | None] = mapped_column(JSON)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ForwardingTopologyDeclaration(Base):
    """Canonical reviewed downstream-to-upstream forwarding declaration."""

    __tablename__ = "forwarding_topology_declarations"
    __table_args__ = (
        UniqueConstraint(
            "created_by_decision_id",
            name="uq_forwarding_topology_declaration_create_decision",
        ),
        UniqueConstraint(
            "retired_by_decision_id",
            name="uq_forwarding_topology_declaration_retire_decision",
        ),
        UniqueConstraint(
            "declaration_sha256",
            name="uq_forwarding_topology_declaration_sha256",
        ),
        CheckConstraint(
            "path_kind IN ('internal', 'border_peer', 'nas_termination')",
            name="ck_forwarding_topology_declaration_kind",
        ),
        CheckConstraint(
            "downstream_role IN "
            "('access', 'aggregation', 'distribution', 'core', 'border', 'nas') "
            "AND (upstream_role IS NULL OR upstream_role IN "
            "('access', 'aggregation', 'distribution', 'core', 'border', 'nas'))",
            name="ck_forwarding_topology_declaration_roles",
        ),
        CheckConstraint(
            "preference > 0",
            name="ck_forwarding_topology_declaration_preference",
        ),
        CheckConstraint(
            "length(declaration_sha256) = 64",
            name="ck_forwarding_topology_declaration_sha256",
        ),
        CheckConstraint(
            "(active AND retired_by_decision_id IS NULL AND retired_at IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL "
            "AND retired_at IS NOT NULL)",
            name="ck_forwarding_topology_declaration_retirement",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "upstream_device_id IS NULL OR downstream_device_id <> upstream_device_id",
            name="ck_forwarding_topology_declaration_distinct_devices",
        ),
        Index(
            "uq_forwarding_topology_active_path_key",
            "path_key",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
        Index(
            "uq_forwarding_topology_active_preference",
            "downstream_device_id",
            "vrf_name",
            "preference",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
        Index(
            "ix_forwarding_topology_active_downstream",
            "downstream_device_id",
            "vrf_name",
            "active",
        ),
        Index(
            "ix_forwarding_topology_active_upstream",
            "upstream_device_id",
            "vrf_name",
            "active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    path_key: Mapped[str] = mapped_column(String(120), nullable=False)
    path_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    downstream_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    downstream_interface_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_interfaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    downstream_pop_site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pop_sites.id", ondelete="RESTRICT"),
        nullable=False,
    )
    downstream_role: Mapped[str] = mapped_column(String(30), nullable=False)
    upstream_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id", ondelete="RESTRICT")
    )
    upstream_interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id", ondelete="RESTRICT")
    )
    upstream_pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id", ondelete="RESTRICT")
    )
    upstream_role: Mapped[str | None] = mapped_column(String(30))
    vrf_name: Mapped[str] = mapped_column(String(120), nullable=False)
    peer_ip: Mapped[str | None] = mapped_column(String(64))
    peer_asn: Mapped[int | None] = mapped_column(BigInteger)
    route_prefix: Mapped[str | None] = mapped_column(String(80))
    next_hop_ip: Mapped[str | None] = mapped_column(String(64))
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id", ondelete="RESTRICT")
    )
    preference: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_owner: Mapped[str] = mapped_column(String(120), nullable=False)
    configuration_intent_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("forwarding_topology_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retired_by_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("forwarding_topology_decisions.id", ondelete="RESTRICT"),
    )
    declaration_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    declared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    downstream_device = relationship(
        "NetworkDevice", foreign_keys=[downstream_device_id]
    )
    downstream_interface = relationship(
        "DeviceInterface", foreign_keys=[downstream_interface_id]
    )
    downstream_pop_site = relationship("PopSite", foreign_keys=[downstream_pop_site_id])
    upstream_device = relationship("NetworkDevice", foreign_keys=[upstream_device_id])
    upstream_interface = relationship(
        "DeviceInterface", foreign_keys=[upstream_interface_id]
    )
    upstream_pop_site = relationship("PopSite", foreign_keys=[upstream_pop_site_id])
    nas_device = relationship("NasDevice")


class ForwardingControlObservation(Base):
    """Immutable normalized BGP-peer or routing-table observation."""

    __tablename__ = "forwarding_control_observations"
    __table_args__ = (
        UniqueConstraint(
            "client_ref", name="uq_forwarding_control_observation_client_ref"
        ),
        UniqueConstraint(
            "observation_sha256",
            name="uq_forwarding_control_observation_sha256",
        ),
        CheckConstraint(
            "source_type IN ('bgp_peer', 'routing_table')",
            name="ck_forwarding_control_observation_source",
        ),
        CheckConstraint(
            "length(observation_sha256) = 64 AND length(source_evidence_sha256) = 64",
            name="ck_forwarding_control_observation_hashes",
        ),
        CheckConstraint(
            "expires_at > observed_at",
            name="ck_forwarding_control_observation_expiry",
        ),
        CheckConstraint(
            "(source_type = 'bgp_peer' "
            "AND peer_ip IS NOT NULL AND peer_asn IS NOT NULL "
            "AND route_prefix IS NULL AND next_hop_ip IS NULL) OR "
            "(source_type = 'routing_table' "
            "AND peer_ip IS NULL AND peer_asn IS NULL "
            "AND route_prefix IS NOT NULL AND next_hop_ip IS NOT NULL)",
            name="ck_forwarding_control_observation_shape",
        ),
        Index(
            "ix_forwarding_control_observation_lookup",
            "source_type",
            "device_id",
            "interface_id",
            "vrf_name",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_ref: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    collector: Mapped[str] = mapped_column(String(120), nullable=False)
    collector_run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    interface_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_interfaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    vrf_name: Mapped[str] = mapped_column(String(120), nullable=False)
    peer_ip: Mapped[str | None] = mapped_column(String(64))
    peer_asn: Mapped[int | None] = mapped_column(BigInteger)
    route_prefix: Mapped[str | None] = mapped_column(String(80))
    next_hop_ip: Mapped[str | None] = mapped_column(String(64))
    source_evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    device = relationship("NetworkDevice")
    interface = relationship("DeviceInterface")


__all__ = [
    "ForwardingControlObservation",
    "ForwardingTopologyDecision",
    "ForwardingTopologyDeclaration",
]
