import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceTeamType(enum.Enum):
    operations = "operations"
    support = "support"
    field_service = "field_service"


class ServiceTeamMemberRole(enum.Enum):
    member = "member"
    lead = "lead"
    manager = "manager"


class ServiceTeam(Base):
    __tablename__ = "service_teams"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    team_type: Mapped[str] = mapped_column(String(40), nullable=False)
    region: Mapped[str | None] = mapped_column(String(80))
    # Staff identity: can reference internal system users or CRM staff people,
    # so keep it a plain UUID instead of a subscriber FK.
    manager_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    erp_department: Mapped[str | None] = mapped_column(String(120), unique=True)
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
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
