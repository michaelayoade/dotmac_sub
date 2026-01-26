import enum
import uuid
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class DeviceRole(enum.Enum):
    core = "core"
    distribution = "distribution"
    access = "access"
    aggregation = "aggregation"
    edge = "edge"
    cpe = "cpe"


class DeviceStatus(enum.Enum):
    online = "online"
    offline = "offline"
    degraded = "degraded"
    maintenance = "maintenance"


class DeviceType(enum.Enum):
    router = "router"
    switch = "switch"
    hub = "hub"
    firewall = "firewall"
    inverter = "inverter"
    access_point = "access_point"
    bridge = "bridge"
    modem = "modem"
    server = "server"
    other = "other"


class InterfaceStatus(enum.Enum):
    up = "up"
    down = "down"
    unknown = "unknown"


class MetricType(enum.Enum):
    cpu = "cpu"
    memory = "memory"
    temperature = "temperature"
    rx_bps = "rx_bps"
    tx_bps = "tx_bps"
    uptime = "uptime"
    custom = "custom"


class AlertSeverity(enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class AlertStatus(enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class AlertOperator(enum.Enum):
    gt = "gt"
    gte = "gte"
    lt = "lt"
    lte = "lte"
    eq = "eq"


class PopSite(Base):
    __tablename__ = "pop_sites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    address_line1: Mapped[str | None] = mapped_column(String(120))
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    devices = relationship("NetworkDevice", back_populates="pop_site")
    masts = relationship("WirelessMast", back_populates="pop_site")
    nas_devices = relationship("NasDevice", back_populates="pop_site")


class NetworkDevice(Base):
    __tablename__ = "network_devices"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_network_devices_hostname"),
        UniqueConstraint("mgmt_ip", name="uq_network_devices_mgmt_ip"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(160))
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    vendor: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    device_type: Mapped[DeviceType | None] = mapped_column(Enum(DeviceType))
    role: Mapped[DeviceRole] = mapped_column(Enum(DeviceRole), default=DeviceRole.edge)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, name="monitoring_devicestatus"), default=DeviceStatus.offline
    )
    ping_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    snmp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    snmp_port: Mapped[int | None] = mapped_column(Integer)
    snmp_version: Mapped[str | None] = mapped_column(String(10))
    snmp_community: Mapped[str | None] = mapped_column(String(255))
    snmp_username: Mapped[str | None] = mapped_column(String(120))
    snmp_auth_protocol: Mapped[str | None] = mapped_column(String(16))
    snmp_auth_secret: Mapped[str | None] = mapped_column(String(255))
    snmp_priv_protocol: Mapped[str | None] = mapped_column(String(16))
    snmp_priv_secret: Mapped[str | None] = mapped_column(String(255))
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ping_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_snmp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snmp_ok: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Capacity tracking
    max_concurrent_subscribers: Mapped[int | None] = mapped_column(Integer)
    current_subscriber_count: Mapped[int] = mapped_column(Integer, default=0)

    # Health tracking
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    pop_site = relationship("PopSite", back_populates="devices")
    interfaces = relationship("DeviceInterface", back_populates="device")
    metrics = relationship("DeviceMetric", back_populates="device")
    alerts = relationship("Alert", back_populates="device")
    alert_rules = relationship("AlertRule", back_populates="device")
    nas_device = relationship("NasDevice", back_populates="network_device", uselist=False)


class DeviceInterface(Base):
    __tablename__ = "device_interfaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[InterfaceStatus] = mapped_column(
        Enum(InterfaceStatus), default=InterfaceStatus.unknown
    )
    speed_mbps: Mapped[int | None] = mapped_column(Integer)
    mac_address: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    device = relationship("NetworkDevice", back_populates="interfaces")
    alerts = relationship("Alert", back_populates="interface")
    alert_rules = relationship("AlertRule", back_populates="interface")
    metrics = relationship("DeviceMetric", back_populates="interface")


class DeviceMetric(Base):
    __tablename__ = "device_metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    metric_type: Mapped[MetricType] = mapped_column(
        Enum(MetricType), default=MetricType.custom
    )
    value: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    device = relationship("NetworkDevice", back_populates="metrics")
    interface = relationship("DeviceInterface", back_populates="metrics")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    metric_type: Mapped[MetricType] = mapped_column(Enum(MetricType), nullable=False)
    operator: Mapped[AlertOperator] = mapped_column(
        Enum(AlertOperator), default=AlertOperator.gt
    )
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    device = relationship("NetworkDevice", back_populates="alert_rules")
    interface = relationship("DeviceInterface", back_populates="alert_rules")
    alerts = relationship("Alert", back_populates="rule")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_rules.id"), nullable=False
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    metric_type: Mapped[MetricType] = mapped_column(Enum(MetricType), nullable=False)
    measured_value: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), default=AlertStatus.open
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    rule = relationship("AlertRule", back_populates="alerts")
    device = relationship("NetworkDevice")
    interface = relationship("DeviceInterface")
    events = relationship("AlertEvent", back_populates="alert")


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=False
    )
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), default=AlertStatus.open
    )
    message: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    alert = relationship("Alert", back_populates="events")
