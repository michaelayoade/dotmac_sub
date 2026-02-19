import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RadiusServer(Base):
    __tablename__ = "radius_servers"
    __table_args__ = (
        UniqueConstraint(
            "host", "auth_port", "acct_port", name="uq_radius_servers_host_ports"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_port: Mapped[int] = mapped_column(Integer, default=1812)
    acct_port: Mapped[int] = mapped_column(Integer, default=1813)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    clients = relationship("RadiusClient", back_populates="server")


class RadiusClient(Base):
    __tablename__ = "radius_clients"
    __table_args__ = (
        UniqueConstraint("server_id", "client_ip", name="uq_radius_clients_server_ip"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_servers.id"), nullable=False
    )
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id")
    )
    client_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    shared_secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    server = relationship("RadiusServer", back_populates="clients")
    nas_device = relationship("NasDevice", back_populates="radius_clients")


class RadiusUser(Base):
    __tablename__ = "radius_users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_radius_users_username"),
        UniqueConstraint("access_credential_id", name="uq_radius_users_access_credential"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    access_credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("access_credentials.id"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    secret_hash: Mapped[str | None] = mapped_column(String(255))
    radius_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    access_credential = relationship("AccessCredential", back_populates="radius_users")
    subscription = relationship("Subscription")
    subscriber = relationship("Subscriber")
    radius_profile = relationship("RadiusProfile")


class RadiusSyncStatus(enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class RadiusSyncJob(Base):
    __tablename__ = "radius_sync_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_servers.id"), nullable=False
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    sync_users: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_nas_clients: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    server = relationship("RadiusServer")
    connector_config = relationship("ConnectorConfig")
    runs = relationship("RadiusSyncRun", back_populates="job")


class RadiusSyncRun(Base):
    __tablename__ = "radius_sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_sync_jobs.id"), nullable=False
    )
    status: Mapped[RadiusSyncStatus] = mapped_column(
        Enum(RadiusSyncStatus), default=RadiusSyncStatus.running
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    users_created: Mapped[int] = mapped_column(Integer, default=0)
    users_updated: Mapped[int] = mapped_column(Integer, default=0)
    clients_created: Mapped[int] = mapped_column(Integer, default=0)
    clients_updated: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict | None] = mapped_column(JSON)

    job = relationship("RadiusSyncJob", back_populates="runs")
