import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Tr069Event(enum.Enum):
    boot = "boot"
    bootstrap = "bootstrap"
    periodic = "periodic"
    value_change = "value_change"
    connection_request = "connection_request"
    transfer_complete = "transfer_complete"
    diagnostics_complete = "diagnostics_complete"


class Tr069JobStatus(enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class Tr069AcsServer(Base):
    __tablename__ = "tr069_acs_servers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    devices = relationship("Tr069CpeDevice", back_populates="acs_server")


class Tr069CpeDevice(Base):
    __tablename__ = "tr069_cpe_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    acs_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_acs_servers.id"), nullable=False
    )
    cpe_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cpe_devices.id")
    )
    serial_number: Mapped[str | None] = mapped_column(String(120))
    oui: Mapped[str | None] = mapped_column(String(8))
    product_class: Mapped[str | None] = mapped_column(String(120))
    connection_request_url: Mapped[str | None] = mapped_column(String(255))
    last_inform_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    acs_server = relationship("Tr069AcsServer", back_populates="devices")
    cpe_device = relationship("CPEDevice")
    sessions = relationship("Tr069Session", back_populates="device")
    parameters = relationship("Tr069Parameter", back_populates="device")
    jobs = relationship("Tr069Job", back_populates="device")


class Tr069Session(Base):
    __tablename__ = "tr069_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_cpe_devices.id"), nullable=False
    )
    event_type: Mapped[Tr069Event] = mapped_column(Enum(Tr069Event), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(120))
    inform_payload: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    device = relationship("Tr069CpeDevice", back_populates="sessions")


class Tr069Parameter(Base):
    __tablename__ = "tr069_parameters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_cpe_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    device = relationship("Tr069CpeDevice", back_populates="parameters")


class Tr069Job(Base):
    __tablename__ = "tr069_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_cpe_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    command: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[Tr069JobStatus] = mapped_column(
        Enum(Tr069JobStatus), default=Tr069JobStatus.queued
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    device = relationship("Tr069CpeDevice", back_populates="jobs")
