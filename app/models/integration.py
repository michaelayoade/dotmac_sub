import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
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
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    connector_config = relationship("ConnectorConfig")
    jobs = relationship("IntegrationJob", back_populates="target")


class IntegrationJob(Base):
    __tablename__ = "integration_jobs"

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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    target = relationship("IntegrationTarget", back_populates="jobs")
    runs = relationship("IntegrationRun", back_populates="job")


class IntegrationRun(Base):
    __tablename__ = "integration_runs"

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
    error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    job = relationship("IntegrationJob", back_populates="runs")
