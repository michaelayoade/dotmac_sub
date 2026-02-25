import enum
import uuid
from datetime import UTC, datetime

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
from app.models.catalog import HealthStatus


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


class SpeedTestSource(enum.Enum):
    manual = "manual"
    scheduled = "scheduled"
    api = "api"


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


class DnsThreatSeverity(enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class DnsThreatAction(enum.Enum):
    blocked = "blocked"
    allowed = "allowed"
    monitored = "monitored"


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
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    devices = relationship("NetworkDevice", back_populates="pop_site")
    nas_devices = relationship("NasDevice", back_populates="pop_site")
    contacts = relationship(
        "PopSiteContact",
        back_populates="pop_site",
        cascade="all, delete-orphan",
        order_by="PopSiteContact.created_at.desc()",
    )
    zone = relationship("NetworkZone")
    organization = relationship("Organization")
    reseller = relationship("Reseller")


class PopSiteContact(Base):
    __tablename__ = "pop_site_contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pop_site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pop_site = relationship("PopSite", back_populates="contacts")


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
    parent_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id", ondelete="SET NULL")
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
    send_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    notification_delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
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
    ping_down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snmp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snmp_ok: Mapped[bool | None] = mapped_column(Boolean)
    snmp_down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    splynx_monitoring_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Capacity tracking
    max_concurrent_subscribers: Mapped[int | None] = mapped_column(Integer)
    current_subscriber_count: Mapped[int] = mapped_column(Integer, default=0)

    # Health tracking
    health_status: Mapped[HealthStatus] = mapped_column(
        Enum(HealthStatus, values_callable=lambda x: [e.value for e in x]),
        default=HealthStatus.unknown,
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    pop_site = relationship("PopSite", back_populates="devices")
    parent_device = relationship(
        "NetworkDevice",
        remote_side="NetworkDevice.id",
        back_populates="child_devices",
    )
    child_devices = relationship(
        "NetworkDevice",
        back_populates="parent_device",
    )
    interfaces = relationship("DeviceInterface", back_populates="device")
    metrics = relationship("DeviceMetric", back_populates="device")
    snmp_oids = relationship("NetworkDeviceSnmpOid", back_populates="device")
    bandwidth_graphs = relationship("NetworkDeviceBandwidthGraph", back_populates="device")
    graph_sources = relationship("NetworkDeviceBandwidthGraphSource", back_populates="source_device")
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
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
    value: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    device = relationship("NetworkDevice", back_populates="metrics")
    interface = relationship("DeviceInterface", back_populates="metrics")


class NetworkDeviceSnmpOid(Base):
    __tablename__ = "network_device_snmp_oids"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    oid: Mapped[str] = mapped_column(String(160), nullable=False)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    rrd_data_source_type: Mapped[str] = mapped_column(String(16), default="gauge")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_poll_status: Mapped[str | None] = mapped_column(String(16))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    device = relationship("NetworkDevice", back_populates="snmp_oids")
    graph_sources = relationship("NetworkDeviceBandwidthGraphSource", back_populates="snmp_oid")


class NetworkDeviceBandwidthGraph(Base):
    __tablename__ = "network_device_bandwidth_graphs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    vertical_axis_title: Mapped[str] = mapped_column(String(80), default="Bandwidth")
    height_px: Mapped[int] = mapped_column(Integer, default=150)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    public_token: Mapped[str | None] = mapped_column(String(64), unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    device = relationship("NetworkDevice", back_populates="bandwidth_graphs")
    sources = relationship(
        "NetworkDeviceBandwidthGraphSource",
        back_populates="graph",
        order_by="NetworkDeviceBandwidthGraphSource.sort_order.asc()",
    )


class NetworkDeviceBandwidthGraphSource(Base):
    __tablename__ = "network_device_bandwidth_graph_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    graph_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_device_bandwidth_graphs.id"), nullable=False
    )
    source_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    snmp_oid_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_device_snmp_oids.id"), nullable=False
    )
    factor: Mapped[float] = mapped_column(Float, default=1.0)
    color_hex: Mapped[str] = mapped_column(String(7), default="#22c55e")
    draw_type: Mapped[str] = mapped_column(String(16), default="LINE1")
    stack_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    value_unit: Mapped[str] = mapped_column(String(12), default="Bps")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    graph = relationship("NetworkDeviceBandwidthGraph", back_populates="sources")
    source_device = relationship("NetworkDevice", back_populates="graph_sources")
    snmp_oid = relationship("NetworkDeviceSnmpOid", back_populates="graph_sources")


class SpeedTestResult(Base):
    __tablename__ = "speed_test_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    source: Mapped[SpeedTestSource] = mapped_column(
        Enum(SpeedTestSource), default=SpeedTestSource.manual
    )
    target_label: Mapped[str | None] = mapped_column(String(160))
    provider: Mapped[str | None] = mapped_column(String(120))
    server_name: Mapped[str | None] = mapped_column(String(160))
    external_ip: Mapped[str | None] = mapped_column(String(64))
    download_mbps: Mapped[float] = mapped_column(Float, default=0)
    upload_mbps: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    jitter_ms: Mapped[float | None] = mapped_column(Float)
    packet_loss_pct: Mapped[float | None] = mapped_column(Float)
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")
    network_device = relationship("NetworkDevice")
    pop_site = relationship("PopSite")


class DnsThreatEvent(Base):
    __tablename__ = "dns_threat_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    queried_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    query_type: Mapped[str | None] = mapped_column(String(16))
    source_ip: Mapped[str | None] = mapped_column(String(64))
    destination_ip: Mapped[str | None] = mapped_column(String(64))
    threat_category: Mapped[str | None] = mapped_column(String(80))
    threat_feed: Mapped[str | None] = mapped_column(String(120))
    severity: Mapped[DnsThreatSeverity] = mapped_column(
        Enum(DnsThreatSeverity), default=DnsThreatSeverity.medium
    )
    action: Mapped[DnsThreatAction] = mapped_column(
        Enum(DnsThreatAction), default=DnsThreatAction.blocked
    )
    confidence_score: Mapped[float | None] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber")
    network_device = relationship("NetworkDevice")
    pop_site = relationship("PopSite")


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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    alert = relationship("Alert", back_populates="events")
