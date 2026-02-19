import enum
import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class BuildoutStatus(enum.Enum):
    planned = "planned"
    in_progress = "in_progress"
    ready = "ready"
    not_planned = "not_planned"


class QualificationStatus(enum.Enum):
    eligible = "eligible"
    ineligible = "ineligible"
    needs_buildout = "needs_buildout"


class BuildoutRequestStatus(enum.Enum):
    submitted = "submitted"
    approved = "approved"
    rejected = "rejected"
    canceled = "canceled"


class BuildoutProjectStatus(enum.Enum):
    planned = "planned"
    in_progress = "in_progress"
    blocked = "blocked"
    ready = "ready"
    completed = "completed"
    canceled = "canceled"


class BuildoutMilestoneStatus(enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    blocked = "blocked"
    canceled = "canceled"


class CoverageArea(Base):
    __tablename__ = "coverage_areas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80))
    zone_key: Mapped[str | None] = mapped_column(String(80))
    buildout_status: Mapped[BuildoutStatus] = mapped_column(
        Enum(BuildoutStatus), default=BuildoutStatus.planned
    )
    buildout_window: Mapped[str | None] = mapped_column(String(120))
    serviceable: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    geometry_geojson: Mapped[dict] = mapped_column(JSON, nullable=False)
    geom = mapped_column(Geometry("GEOMETRY", srid=4326), nullable=True)
    min_latitude: Mapped[float | None] = mapped_column(Float)
    max_latitude: Mapped[float | None] = mapped_column(Float)
    min_longitude: Mapped[float | None] = mapped_column(Float)
    max_longitude: Mapped[float | None] = mapped_column(Float)
    constraints: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    qualifications = relationship("ServiceQualification", back_populates="coverage_area")


class ServiceQualification(Base):
    __tablename__ = "service_qualifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    coverage_area_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coverage_areas.id")
    )
    address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    requested_tech: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[QualificationStatus] = mapped_column(
        Enum(QualificationStatus), default=QualificationStatus.ineligible
    )
    buildout_status: Mapped[BuildoutStatus | None] = mapped_column(Enum(BuildoutStatus))
    estimated_install_window: Mapped[str | None] = mapped_column(String(120))
    reasons: Mapped[list | None] = mapped_column(JSON)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    coverage_area = relationship("CoverageArea", back_populates="qualifications")
    address = relationship("Address")
    buildout_requests = relationship(
        "BuildoutRequest", back_populates="qualification"
    )


class BuildoutRequest(Base):
    __tablename__ = "buildout_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    qualification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_qualifications.id")
    )
    coverage_area_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coverage_areas.id")
    )
    address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    requested_by: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[BuildoutRequestStatus] = mapped_column(
        Enum(BuildoutRequestStatus), default=BuildoutRequestStatus.submitted
    )
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    qualification = relationship("ServiceQualification", back_populates="buildout_requests")
    coverage_area = relationship("CoverageArea")
    address = relationship("Address")
    project = relationship("BuildoutProject", back_populates="request", uselist=False)


class BuildoutProject(Base):
    __tablename__ = "buildout_projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buildout_requests.id")
    )
    coverage_area_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coverage_areas.id")
    )
    address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    status: Mapped[BuildoutProjectStatus] = mapped_column(
        Enum(BuildoutProjectStatus), default=BuildoutProjectStatus.planned
    )
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    target_ready_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    request = relationship("BuildoutRequest", back_populates="project")
    coverage_area = relationship("CoverageArea")
    address = relationship("Address")
    milestones = relationship("BuildoutMilestone", back_populates="project")
    updates = relationship("BuildoutUpdate", back_populates="project")


class BuildoutMilestone(Base):
    __tablename__ = "buildout_milestones"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buildout_projects.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[BuildoutMilestoneStatus] = mapped_column(
        Enum(BuildoutMilestoneStatus), default=BuildoutMilestoneStatus.pending
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    project = relationship("BuildoutProject", back_populates="milestones")


class BuildoutUpdate(Base):
    __tablename__ = "buildout_updates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("buildout_projects.id"), nullable=False
    )
    status: Mapped[BuildoutProjectStatus] = mapped_column(
        Enum(BuildoutProjectStatus), default=BuildoutProjectStatus.planned
    )
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    project = relationship("BuildoutProject", back_populates="updates")
