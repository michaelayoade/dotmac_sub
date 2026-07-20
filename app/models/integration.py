import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class IntegrationTargetType(enum.Enum):
    radius = "radius"
    crm = "crm"
    billing = "billing"
    n8n = "n8n"
    custom = "custom"


class IntegrationJobType(enum.Enum):
    sync = "sync"
    export = "export"
    import_ = "import"


class IntegrationScheduleType(enum.Enum):
    manual = "manual"
    interval = "interval"


class IntegrationRunStatus(enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class IntegrationTarget(Base):
    __tablename__ = "integration_targets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    target_type: Mapped[IntegrationTargetType] = mapped_column(
        Enum(IntegrationTargetType), default=IntegrationTargetType.custom
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    jobs = relationship("IntegrationJob", back_populates="target")


class IntegrationJob(Base):
    __tablename__ = "integration_jobs"
    __table_args__ = (
        CheckConstraint(
            "NOT is_active OR capability_binding_id IS NOT NULL",
            name="ck_integration_jobs_active_binding",
        ),
        Index(
            "ix_integration_jobs_capability_active",
            "capability_binding_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_targets.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    job_type: Mapped[IntegrationJobType] = mapped_column(
        Enum(IntegrationJobType), default=IntegrationJobType.sync
    )
    schedule_type: Mapped[IntegrationScheduleType] = mapped_column(
        Enum(IntegrationScheduleType), default=IntegrationScheduleType.manual
    )
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    capability_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "integration_capability_bindings.id",
            name="fk_integration_jobs_capability_binding",
        ),
    )
    entity_type: Mapped[str | None] = mapped_column(String(80))
    direction: Mapped[str | None] = mapped_column(String(24))
    trigger_mode: Mapped[str | None] = mapped_column(String(24))
    mapping_config: Mapped[dict | None] = mapped_column(JSON)
    filter_config: Mapped[dict | None] = mapped_column(JSON)
    conflict_policy: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    target = relationship("IntegrationTarget", back_populates="jobs")
    capability_binding = relationship("IntegrationCapabilityBinding")
    runs = relationship("IntegrationRun", back_populates="job")


class IntegrationRun(Base):
    __tablename__ = "integration_runs"
    __table_args__ = (
        Index(
            "ix_integration_runs_binding_started",
            "capability_binding_id",
            "started_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_jobs.id"), nullable=False
    )
    status: Mapped[IntegrationRunStatus] = mapped_column(
        Enum(IntegrationRunStatus), default=IntegrationRunStatus.running
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str | None] = mapped_column(String(32))
    requested_by: Mapped[str | None] = mapped_column(String(160))
    installation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_installations.id")
    )
    capability_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_capability_bindings.id")
    )
    config_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_config_revisions.id")
    )
    capability_id: Mapped[str | None] = mapped_column(String(160))
    connector_key: Mapped[str | None] = mapped_column(String(120))
    connector_version: Mapped[str | None] = mapped_column(String(32))
    manifest_digest: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    job = relationship("IntegrationJob", back_populates="runs")
    installation = relationship("IntegrationInstallation")
    capability_binding = relationship("IntegrationCapabilityBinding")
    config_revision = relationship("IntegrationConfigRevision")
    records = relationship("IntegrationRecord", back_populates="run")


class IntegrationRecord(Base):
    __tablename__ = "integration_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_runs.id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[str] = mapped_column(String(24), nullable=False)
    local_id: Mapped[str | None] = mapped_column(String(120))
    remote_id: Mapped[str | None] = mapped_column(String(120))
    remote_number: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    payload_snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    run = relationship("IntegrationRun", back_populates="records")
