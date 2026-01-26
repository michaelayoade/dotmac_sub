import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SnmpVersion(enum.Enum):
    v2c = "v2c"
    v3 = "v3"


class SnmpAuthProtocol(enum.Enum):
    none = "none"
    md5 = "md5"
    sha = "sha"


class SnmpPrivProtocol(enum.Enum):
    none = "none"
    des = "des"
    aes = "aes"


class SnmpCredential(Base):
    __tablename__ = "snmp_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[SnmpVersion] = mapped_column(Enum(SnmpVersion), nullable=False)
    community_hash: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(120))
    auth_protocol: Mapped[SnmpAuthProtocol] = mapped_column(
        Enum(SnmpAuthProtocol), default=SnmpAuthProtocol.none
    )
    auth_secret_hash: Mapped[str | None] = mapped_column(String(255))
    priv_protocol: Mapped[SnmpPrivProtocol] = mapped_column(
        Enum(SnmpPrivProtocol), default=SnmpPrivProtocol.none
    )
    priv_secret_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    targets = relationship("SnmpTarget", back_populates="credential")


class SnmpTarget(Base):
    __tablename__ = "snmp_targets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    hostname: Mapped[str | None] = mapped_column(String(160))
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer, default=161)
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snmp_credentials.id"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    device = relationship("NetworkDevice")
    credential = relationship("SnmpCredential", back_populates="targets")
    pollers = relationship("SnmpPoller", back_populates="target")


class SnmpOid(Base):
    __tablename__ = "snmp_oids"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    oid: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    pollers = relationship("SnmpPoller", back_populates="oid")


class SnmpPoller(Base):
    __tablename__ = "snmp_pollers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snmp_targets.id"), nullable=False
    )
    oid_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snmp_oids.id"), nullable=False
    )
    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    target = relationship("SnmpTarget", back_populates="pollers")
    oid = relationship("SnmpOid", back_populates="pollers")
    readings = relationship("SnmpReading", back_populates="poller")


class SnmpReading(Base):
    __tablename__ = "snmp_readings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    poller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snmp_pollers.id"), nullable=False
    )
    value: Mapped[int] = mapped_column(Integer, default=0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    poller = relationship("SnmpPoller", back_populates="readings")
