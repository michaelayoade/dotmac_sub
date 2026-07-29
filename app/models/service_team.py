import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceTeamType(enum.Enum):
    operations = "operations"
    billing = "billing"
    support = "support"
    field_service = "field_service"
    project_management = "project_management"


class ServiceTeamMemberRole(enum.Enum):
    """Legacy scalar role retained only for expand/shadow compatibility."""

    member = "member"
    lead = "lead"
    manager = "manager"


class ServiceTeamCapabilityKey(str, enum.Enum):
    operations_general = "operations.general"
    support_tickets = "support.tickets"
    field_service_work_orders = "field_service.work_orders"
    network_outages_coordinate = "network.outages.coordinate"
    network_outages_observe = "network.outages.observe"
    billing_operations = "billing.operations"
    projects_manage = "projects.manage"
    communications_inbox = "communications.inbox"


class ServiceTeamResponsibilityKey(str, enum.Enum):
    accountable_manager = "accountable_manager"
    queue_lead = "queue_lead"
    agent = "agent"
    dispatcher = "dispatcher"
    on_call = "on_call"


class ServiceTeamRelationshipType(str, enum.Enum):
    organizational_parent = "organizational_parent"


class ServiceTeamScopeType(str, enum.Enum):
    geo_area = "geo_area"


class ServiceTeam(Base):
    __tablename__ = "service_teams"
    __table_args__ = (
        Index("ux_service_teams_name_ci", text("lower(name)"), unique=True),
        UniqueConstraint(
            "workforce_system",
            "workforce_department_reference",
            name="uq_service_teams_workforce_system_reference",
        ),
        CheckConstraint(
            "(workforce_system IS NULL) = (workforce_department_reference IS NULL)",
            name="ck_service_teams_workforce_reference_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    team_type: Mapped[str] = mapped_column(String(40), nullable=False)
    region: Mapped[str | None] = mapped_column(String(80))
    manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "parties.id",
            name="fk_service_teams_manager_person_id_parties",
            ondelete="RESTRICT",
        ),
    )
    workforce_system: Mapped[str | None] = mapped_column(String(40))
    workforce_department_reference: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    members = relationship(
        "ServiceTeamMember", back_populates="team", cascade="all, delete-orphan"
    )
    capabilities = relationship(
        "ServiceTeamCapability",
        back_populates="team",
        cascade="all, delete-orphan",
    )
    scope_bindings = relationship(
        "ServiceTeamScopeBinding",
        back_populates="team",
        cascade="all, delete-orphan",
    )
    external_references = relationship(
        "ServiceTeamExternalReference",
        back_populates="team",
        cascade="all, delete-orphan",
    )


class ServiceTeamMember(Base):
    __tablename__ = "service_team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "person_id", name="uq_service_team_member"),
        Index("ix_service_team_members_person_id", "person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "parties.id",
            name="fk_service_team_members_person_id_parties",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    # Legacy shadow field. New operational decisions consume
    # ServiceTeamMemberResponsibility rows.
    role: Mapped[str] = mapped_column(
        String(40),
        default=ServiceTeamMemberRole.member.value,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    team = relationship("ServiceTeam", back_populates="members")
    responsibilities = relationship(
        "ServiceTeamMemberResponsibility",
        back_populates="membership",
        cascade="all, delete-orphan",
    )


class ServiceTeamCapabilityDefinition(Base):
    __tablename__ = "service_team_capability_definitions"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    contract_owner: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ServiceTeamCapability(Base):
    __tablename__ = "service_team_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "capability_key",
            name="uq_service_team_capability",
        ),
        Index(
            "ix_service_team_capabilities_lookup",
            "capability_key",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability_key: Mapped[str] = mapped_column(
        String(80),
        ForeignKey(
            "service_team_capability_definitions.key",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    team = relationship("ServiceTeam", back_populates="capabilities")
    definition = relationship("ServiceTeamCapabilityDefinition")


class ServiceTeamResponsibilityDefinition(Base):
    __tablename__ = "service_team_responsibility_definitions"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    operational_scope: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ServiceTeamMemberResponsibility(Base):
    __tablename__ = "service_team_member_responsibilities"
    __table_args__ = (
        UniqueConstraint(
            "membership_id",
            "responsibility_key",
            name="uq_service_team_member_responsibility",
        ),
        Index(
            "ix_service_team_member_responsibilities_lookup",
            "responsibility_key",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_team_members.id", ondelete="CASCADE"),
        nullable=False,
    )
    responsibility_key: Mapped[str] = mapped_column(
        String(80),
        ForeignKey(
            "service_team_responsibility_definitions.key",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    membership = relationship(
        "ServiceTeamMember",
        back_populates="responsibilities",
    )
    definition = relationship("ServiceTeamResponsibilityDefinition")


class ServiceTeamRelationship(Base):
    __tablename__ = "service_team_relationships"
    __table_args__ = (
        UniqueConstraint(
            "parent_team_id",
            "child_team_id",
            "relationship_type",
            name="uq_service_team_relationship",
        ),
        CheckConstraint(
            "parent_team_id <> child_team_id",
            name="ck_service_team_relationship_not_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    child_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    parent_team = relationship("ServiceTeam", foreign_keys=[parent_team_id])
    child_team = relationship("ServiceTeam", foreign_keys=[child_team_id])


class ServiceTeamScopeBinding(Base):
    __tablename__ = "service_team_scope_bindings"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "scope_type",
            "geo_area_id",
            name="uq_service_team_scope_binding",
        ),
        CheckConstraint(
            "scope_type = 'geo_area' AND geo_area_id IS NOT NULL",
            name="ck_service_team_scope_binding_typed_target",
        ),
        Index(
            "ix_service_team_scope_bindings_geo_area",
            "geo_area_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope_type: Mapped[str] = mapped_column(
        String(40),
        default=ServiceTeamScopeType.geo_area.value,
        nullable=False,
    )
    geo_area_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("geo_areas.id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    team = relationship("ServiceTeam", back_populates="scope_bindings")
    geo_area = relationship("GeoArea")


class ServiceTeamExternalReference(Base):
    __tablename__ = "service_team_external_references"
    __table_args__ = (
        UniqueConstraint(
            "system",
            "entity_type",
            "external_reference",
            name="uq_service_team_external_reference",
        ),
        UniqueConstraint(
            "team_id",
            "system",
            "entity_type",
            name="uq_service_team_external_reference_kind",
        ),
        Index(
            "ix_service_team_external_references_team",
            "team_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    system: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    external_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    team = relationship("ServiceTeam", back_populates="external_references")
