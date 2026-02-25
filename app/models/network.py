import enum
import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.db import Base


class DeviceType(enum.Enum):
    ont = "ont"
    router = "router"
    modem = "modem"
    cpe = "cpe"


class DeviceStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    retired = "retired"


class PortType(enum.Enum):
    pon = "pon"
    ethernet = "ethernet"
    wifi = "wifi"
    mgmt = "mgmt"


class PortStatus(enum.Enum):
    up = "up"
    down = "down"
    disabled = "disabled"


class IPVersion(enum.Enum):
    ipv4 = "ipv4"
    ipv6 = "ipv6"


class HardwareUnitStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    failed = "failed"
    unknown = "unknown"


class OltPortType(enum.Enum):
    pon = "pon"
    uplink = "uplink"
    ethernet = "ethernet"
    mgmt = "mgmt"


class FiberEndpointType(enum.Enum):
    olt_port = "olt_port"
    splitter_port = "splitter_port"
    fdh = "fdh"
    ont = "ont"
    splice_closure = "splice_closure"
    other = "other"


class ODNEndpointType(enum.Enum):
    fdh = "fdh"
    splitter = "splitter"
    splitter_port = "splitter_port"
    pon_port = "pon_port"
    olt_port = "olt_port"
    ont = "ont"
    terminal = "terminal"
    splice_closure = "splice_closure"
    other = "other"


class FiberSegmentType(enum.Enum):
    feeder = "feeder"
    distribution = "distribution"
    drop = "drop"


class FiberStrandStatus(enum.Enum):
    available = "available"
    in_use = "in_use"
    reserved = "reserved"
    damaged = "damaged"
    retired = "retired"


class SplitterPortType(enum.Enum):
    input = "input"
    output = "output"


class PonType(enum.Enum):
    gpon = "gpon"
    epon = "epon"


class GponChannel(enum.Enum):
    gpon = "gpon"
    xg_pon = "xg_pon"
    xgs_pon = "xgs_pon"


class OnuCapability(enum.Enum):
    bridging = "bridging"
    routing = "routing"
    bridging_routing = "bridging_routing"


class OnuMode(enum.Enum):
    routing = "routing"
    bridging = "bridging"


class WanMode(enum.Enum):
    dhcp = "dhcp"
    static_ip = "static_ip"
    pppoe = "pppoe"
    setup_via_onu = "setup_via_onu"


class ConfigMethod(enum.Enum):
    omci = "omci"
    tr069 = "tr069"


class IpProtocol(enum.Enum):
    ipv4 = "ipv4"
    dual_stack = "dual_stack"


class MgmtIpMode(enum.Enum):
    inactive = "inactive"
    static_ip = "static_ip"
    dhcp = "dhcp"


class SpeedProfileDirection(enum.Enum):
    download = "download"
    upload = "upload"


class SpeedProfileType(enum.Enum):
    internet = "internet"
    management = "management"


class OnuOnlineStatus(enum.Enum):
    online = "online"
    offline = "offline"
    unknown = "unknown"


class OnuOfflineReason(enum.Enum):
    power_fail = "power_fail"
    los = "los"
    dying_gasp = "dying_gasp"
    unknown = "unknown"


class VlanPurpose(enum.Enum):
    internet = "internet"
    management = "management"
    tr069 = "tr069"
    iptv = "iptv"
    voip = "voip"
    other = "other"


class CPEDevice(Base):
    __tablename__ = "cpe_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    device_type: Mapped[DeviceType] = mapped_column(
        Enum(DeviceType), default=DeviceType.ont
    )
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, name="cpe_devicestatus"), default=DeviceStatus.active
    )
    serial_number: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    vendor: Mapped[str | None] = mapped_column(String(120))
    mac_address: Mapped[str | None] = mapped_column(String(64))
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="cpe_devices")
    subscription = relationship("Subscription", back_populates="cpe_devices")
    service_address = relationship("Address")
    ports = relationship(
        "Port",
        back_populates="device",
        primaryjoin=lambda: CPEDevice.id == foreign(Port.device_id),
    )


class Port(Base):
    __tablename__ = "ports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Note: this is intentionally not a hard FK. Some legacy callers treat
    # `device_id` / `olt_id` as an identifier for non-CPE devices.
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    port_number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    port_type: Mapped[PortType] = mapped_column(
        Enum(PortType), default=PortType.ethernet
    )
    status: Mapped[PortStatus] = mapped_column(
        Enum(PortStatus), default=PortStatus.down
    )
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    device = relationship(
        "CPEDevice",
        back_populates="ports",
        primaryjoin=lambda: foreign(Port.device_id) == CPEDevice.id,
    )
    vlans = relationship("PortVlan", back_populates="port")

    @hybrid_property
    def is_active(self) -> bool:
        return self.status != PortStatus.disabled

    @property
    def olt_id(self) -> uuid.UUID:
        """Backwards-compat alias for legacy code/tests."""
        return self.device_id

    @olt_id.setter
    def olt_id(self, value: uuid.UUID) -> None:
        self.device_id = value


class Vlan(Base):
    __tablename__ = "vlans"
    __table_args__ = (
        UniqueConstraint("region_id", "tag", name="uq_vlans_region_tag"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    region_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id"), nullable=False
    )
    tag: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[VlanPurpose | None] = mapped_column(
        Enum(VlanPurpose, name="vlanpurpose", create_constraint=False),
        nullable=True,
    )
    dhcp_snooping: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    port_links = relationship("PortVlan", back_populates="vlan")
    region = relationship("RegionZone")


class PortVlan(Base):
    __tablename__ = "port_vlans"
    __table_args__ = (UniqueConstraint("port_id", "vlan_id", name="uq_port_vlans_port_vlan"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ports.id"), nullable=False
    )
    vlan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vlans.id"), nullable=False
    )
    is_tagged: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    port = relationship("Port", back_populates="vlans")
    vlan = relationship("Vlan", back_populates="port_links")


class IPAssignment(Base):
    __tablename__ = "ip_assignments"
    __table_args__ = (
        UniqueConstraint(
            "ipv4_address_id", name="uq_ip_assignments_ipv4_address_id"
        ),
        UniqueConstraint(
            "ipv6_address_id", name="uq_ip_assignments_ipv6_address_id"
        ),
        CheckConstraint(
            "(ip_version = 'ipv4' AND ipv4_address_id IS NOT NULL AND ipv6_address_id IS NULL) OR "
            "(ip_version = 'ipv6' AND ipv6_address_id IS NOT NULL AND ipv4_address_id IS NULL)",
            name="ck_ip_assignments_version_address",
        ),
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
    subscription_add_on_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscription_add_ons.id")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    ip_version: Mapped[IPVersion] = mapped_column(
        Enum(IPVersion), default=IPVersion.ipv4
    )
    ipv4_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ipv4_addresses.id")
    )
    ipv6_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ipv6_addresses.id")
    )
    prefix_length: Mapped[int | None] = mapped_column(Integer)
    gateway: Mapped[str | None] = mapped_column(String(64))
    dns_primary: Mapped[str | None] = mapped_column(String(64))
    dns_secondary: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", back_populates="ip_assignments")
    subscription = relationship("Subscription", back_populates="ip_assignments")
    subscription_add_on = relationship("SubscriptionAddOn")
    service_address = relationship("Address")
    ipv4_address = relationship("IPv4Address", back_populates="assignment")
    ipv6_address = relationship("IPv6Address", back_populates="assignment")


class IpPool(Base):
    __tablename__ = "ip_pools"
    __table_args__ = (UniqueConstraint("name", name="uq_ip_pools_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    ip_version: Mapped[IPVersion] = mapped_column(
        Enum(IPVersion), default=IPVersion.ipv4
    )
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    gateway: Mapped[str | None] = mapped_column(String(64))
    dns_primary: Mapped[str | None] = mapped_column(String(64))
    dns_secondary: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    blocks = relationship("IpBlock", back_populates="pool")
    ipv4_addresses = relationship("IPv4Address", back_populates="pool")
    ipv6_addresses = relationship("IPv6Address", back_populates="pool")


class IpBlock(Base):
    __tablename__ = "ip_blocks"
    __table_args__ = (
        UniqueConstraint("pool_id", "cidr", name="uq_ip_blocks_pool_cidr"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_pools.id"), nullable=False
    )
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    pool = relationship("IpPool", back_populates="blocks")


class IPv4Address(Base):
    __tablename__ = "ipv4_addresses"
    __table_args__ = (
        UniqueConstraint("address", name="uq_ipv4_addresses_address"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    address: Mapped[str] = mapped_column(String(15), nullable=False)
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_pools.id")
    )
    is_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    assignment = relationship(
        "IPAssignment", back_populates="ipv4_address", uselist=False
    )
    pool = relationship("IpPool", back_populates="ipv4_addresses")


class IPv6Address(Base):
    __tablename__ = "ipv6_addresses"
    __table_args__ = (
        UniqueConstraint("address", name="uq_ipv6_addresses_address"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_pools.id")
    )
    is_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    assignment = relationship(
        "IPAssignment", back_populates="ipv6_address", uselist=False
    )
    pool = relationship("IpPool", back_populates="ipv6_addresses")


class OLTDevice(Base):
    __tablename__ = "olt_devices"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_olt_devices_hostname"),
        UniqueConstraint("mgmt_ip", name="uq_olt_devices_mgmt_ip"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(160))
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    vendor: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    pon_ports = relationship("PonPort", back_populates="olt")
    power_units = relationship("OltPowerUnit", back_populates="olt")
    shelves = relationship("OltShelf", back_populates="olt")
    config_backups = relationship("OltConfigBackup", back_populates="olt")


class OltConfigBackupType(enum.Enum):
    auto = "auto"
    manual = "manual"


class OltConfigBackup(Base):
    """OLT running-config backup snapshot."""

    __tablename__ = "olt_config_backups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False, index=True
    )
    backup_type: Mapped[OltConfigBackupType] = mapped_column(
        Enum(OltConfigBackupType, name="oltconfigbackuptype", create_constraint=False),
        nullable=False,
        default=OltConfigBackupType.auto,
    )
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice", back_populates="config_backups")


class OltShelf(Base):
    __tablename__ = "olt_shelves"
    __table_args__ = (
        UniqueConstraint("olt_id", "shelf_number", name="uq_olt_shelves_olt_shelf_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False
    )
    shelf_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice", back_populates="shelves")
    cards = relationship("OltCard", back_populates="shelf")


class OltCard(Base):
    __tablename__ = "olt_cards"
    __table_args__ = (
        UniqueConstraint("shelf_id", "slot_number", name="uq_olt_cards_shelf_slot_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    shelf_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_shelves.id"), nullable=False
    )
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    card_type: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    shelf = relationship("OltShelf", back_populates="cards")
    ports = relationship("OltCardPort", back_populates="card")


class OltCardPort(Base):
    __tablename__ = "olt_card_ports"
    __table_args__ = (
        UniqueConstraint("card_id", "port_number", name="uq_olt_card_ports_card_port_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_cards.id"), nullable=False
    )
    port_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    port_type: Mapped[OltPortType] = mapped_column(
        Enum(OltPortType), default=OltPortType.pon
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    card = relationship("OltCard", back_populates="ports")
    pon_port = relationship("PonPort", back_populates="olt_card_port", uselist=False)
    sfp_modules = relationship("OltSfpModule", back_populates="olt_card_port")


class PonPort(Base):
    __tablename__ = "pon_ports"
    __table_args__ = (
        UniqueConstraint("olt_id", "name", name="uq_pon_ports_olt_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False
    )
    olt_card_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_card_ports.id")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    port_number: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice", back_populates="pon_ports")
    olt_card_port = relationship("OltCardPort", back_populates="pon_port")
    ont_assignments = relationship("OntAssignment", back_populates="pon_port")
    splitter_link = relationship(
        "PonPortSplitterLink", back_populates="pon_port", uselist=False
    )

    @property
    def card_id(self) -> uuid.UUID | None:
        """Backwards-compat: expose card_id via the linked OLT card port."""
        if self.olt_card_port:
            return self.olt_card_port.card_id
        return None


class OltPowerUnit(Base):
    __tablename__ = "olt_power_units"
    __table_args__ = (
        UniqueConstraint("olt_id", "slot", name="uq_olt_power_units_olt_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False
    )
    slot: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[HardwareUnitStatus | None] = mapped_column(
        Enum(HardwareUnitStatus, values_callable=lambda x: [e.value for e in x]),
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice", back_populates="power_units")


class OltSfpModule(Base):
    __tablename__ = "olt_sfp_modules"
    __table_args__ = (
        UniqueConstraint("olt_card_port_id", "serial_number", name="uq_olt_sfp_modules_port_serial"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_card_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_card_ports.id"), nullable=False
    )
    vendor: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    wavelength_nm: Mapped[int | None] = mapped_column(Integer)
    rx_power_dbm: Mapped[float | None] = mapped_column(Float)
    tx_power_dbm: Mapped[float | None] = mapped_column(Float)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    olt_card_port = relationship("OltCardPort", back_populates="sfp_modules")


class OntUnit(Base):
    __tablename__ = "ont_units"
    __table_args__ = (
        UniqueConstraint("serial_number", name="uq_ont_units_serial_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    serial_number: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    vendor: Mapped[str | None] = mapped_column(String(120))
    firmware_version: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Optical signal monitoring fields
    onu_rx_signal_dbm: Mapped[float | None] = mapped_column(Float)
    olt_rx_signal_dbm: Mapped[float | None] = mapped_column(Float)
    distance_meters: Mapped[int | None] = mapped_column(Integer)
    signal_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    online_status: Mapped[OnuOnlineStatus] = mapped_column(
        Enum(OnuOnlineStatus, name="onuonlinestatus", create_constraint=False),
        default=OnuOnlineStatus.unknown,
        server_default="unknown",
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offline_reason: Mapped[OnuOfflineReason | None] = mapped_column(
        Enum(OnuOfflineReason, name="onuofflinereason", create_constraint=False),
    )
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    onu_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("onu_types.id")
    )
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id")
    )
    pon_type: Mapped[PonType | None] = mapped_column(
        Enum(PonType, name="pontype", create_constraint=False),
    )
    gpon_channel: Mapped[GponChannel | None] = mapped_column(
        Enum(GponChannel, name="gponchannel", create_constraint=False),
    )
    board: Mapped[str | None] = mapped_column(String(60))
    port: Mapped[str | None] = mapped_column(String(60))
    onu_mode: Mapped[OnuMode | None] = mapped_column(
        Enum(OnuMode, name="onumode", create_constraint=False),
    )
    user_vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vlans.id")
    )
    splitter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitters.id")
    )
    splitter_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitter_ports.id")
    )
    download_speed_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speed_profiles.id")
    )
    upload_speed_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speed_profiles.id")
    )
    name: Mapped[str | None] = mapped_column(String(200))
    address_or_comment: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(120))
    use_gps: Mapped[bool] = mapped_column(Boolean, default=False)
    gps_latitude: Mapped[float | None] = mapped_column(Float)
    gps_longitude: Mapped[float | None] = mapped_column(Float)
    # ONU mode configuration
    wan_vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vlans.id")
    )
    wan_mode: Mapped[WanMode | None] = mapped_column(
        Enum(WanMode, name="wanmode", create_constraint=False),
    )
    config_method: Mapped[ConfigMethod | None] = mapped_column(
        Enum(ConfigMethod, name="configmethod", create_constraint=False),
    )
    ip_protocol: Mapped[IpProtocol | None] = mapped_column(
        Enum(IpProtocol, name="ipprotocol", create_constraint=False),
    )
    pppoe_username: Mapped[str | None] = mapped_column(String(120))
    pppoe_password: Mapped[str | None] = mapped_column(String(120))
    wan_remote_access: Mapped[bool] = mapped_column(Boolean, default=False)
    # Management IP configuration
    tr069_acs_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_acs_servers.id")
    )
    mgmt_ip_mode: Mapped[MgmtIpMode | None] = mapped_column(
        Enum(MgmtIpMode, name="mgmtipmode", create_constraint=False),
    )
    mgmt_vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vlans.id")
    )
    mgmt_ip_address: Mapped[str | None] = mapped_column(String(64))
    mgmt_remote_access: Mapped[bool] = mapped_column(Boolean, default=False)
    voip_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    assignments = relationship("OntAssignment", back_populates="ont_unit")
    zone = relationship("NetworkZone", back_populates="ont_units")
    onu_type = relationship("OnuType", back_populates="ont_units")
    olt_device = relationship("OLTDevice")
    user_vlan = relationship("Vlan", foreign_keys=[user_vlan_id])
    wan_vlan = relationship("Vlan", foreign_keys=[wan_vlan_id])
    mgmt_vlan = relationship("Vlan", foreign_keys=[mgmt_vlan_id])
    splitter = relationship("Splitter")
    splitter_port_rel = relationship("SplitterPort")
    download_speed_profile = relationship("SpeedProfile", foreign_keys=[download_speed_profile_id])
    upload_speed_profile = relationship("SpeedProfile", foreign_keys=[upload_speed_profile_id])
    tr069_acs_server = relationship("Tr069AcsServer")


class OntAssignment(Base):
    __tablename__ = "ont_assignments"
    __table_args__ = (
        Index(
            "ix_ont_assignments_active_unit",
            "ont_unit_id",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id"), nullable=False
    )
    pon_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    ont_unit = relationship("OntUnit", back_populates="assignments")
    pon_port = relationship("PonPort", back_populates="ont_assignments")
    subscriber = relationship("Subscriber", back_populates="ont_assignments")
    subscription = relationship("Subscription", back_populates="ont_assignments")
    service_address = relationship("Address")


class NetworkZone(Base):
    """Geographic zone for organizing network infrastructure."""

    __tablename__ = "network_zones"
    __table_args__ = (
        UniqueConstraint("name", name="uq_network_zones_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    parent = relationship("NetworkZone", remote_side="NetworkZone.id", backref="children")
    ont_units = relationship("OntUnit", back_populates="zone")
    splitters = relationship("Splitter", back_populates="zone")
    fdh_cabinets = relationship("FdhCabinet", back_populates="zone")


class FdhCabinet(Base):
    __tablename__ = "fdh_cabinets"
    __table_args__ = (
        UniqueConstraint("code", name="uq_fdh_cabinets_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80))
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id")
    )
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    splitters = relationship("Splitter", back_populates="fdh")
    region = relationship("RegionZone")
    zone = relationship("NetworkZone", back_populates="fdh_cabinets")


class Splitter(Base):
    __tablename__ = "splitters"
    __table_args__ = (
        UniqueConstraint("fdh_id", "name", name="uq_splitters_fdh_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fdh_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fdh_cabinets.id")
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    splitter_ratio: Mapped[str | None] = mapped_column(String(40))
    input_ports: Mapped[int] = mapped_column(Integer, default=1)
    output_ports: Mapped[int] = mapped_column(Integer, default=8)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    fdh = relationship("FdhCabinet", back_populates="splitters")
    ports = relationship("SplitterPort", back_populates="splitter")
    zone = relationship("NetworkZone", back_populates="splitters")


class SplitterPort(Base):
    __tablename__ = "splitter_ports"
    __table_args__ = (
        UniqueConstraint("splitter_id", "port_number", name="uq_splitter_ports_splitter_port_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splitter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitters.id"), nullable=False
    )
    port_number: Mapped[int] = mapped_column(Integer, nullable=False)
    port_type: Mapped[SplitterPortType] = mapped_column(
        Enum(SplitterPortType), default=SplitterPortType.output
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    splitter = relationship("Splitter", back_populates="ports")
    assignments = relationship("SplitterPortAssignment", back_populates="splitter_port")
    pon_links = relationship("PonPortSplitterLink", back_populates="splitter_port")


class SplitterPortAssignment(Base):
    __tablename__ = "splitter_port_assignments"
    __table_args__ = (
        UniqueConstraint(
            "splitter_port_id",
            "active",
            name="uq_splitter_port_assignments_port_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splitter_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitter_ports.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    splitter_port = relationship("SplitterPort", back_populates="assignments")
    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")
    service_address = relationship("Address")


class FiberStrand(Base):
    __tablename__ = "fiber_strands"
    __table_args__ = (
        UniqueConstraint("cable_name", "strand_number", name="uq_fiber_strands_cable_strand"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cable_name: Mapped[str] = mapped_column(String(160), nullable=False)
    strand_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[FiberStrandStatus] = mapped_column(
        Enum(FiberStrandStatus), default=FiberStrandStatus.available
    )
    upstream_type: Mapped[FiberEndpointType | None] = mapped_column(Enum(FiberEndpointType))
    upstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    downstream_type: Mapped[FiberEndpointType | None] = mapped_column(Enum(FiberEndpointType))
    downstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    splices_from = relationship(
        "FiberSplice", back_populates="from_strand", foreign_keys="FiberSplice.from_strand_id"
    )
    splices_to = relationship(
        "FiberSplice", back_populates="to_strand", foreign_keys="FiberSplice.to_strand_id"
    )
    segments = relationship("FiberSegment", back_populates="fiber_strand")

    @property
    def segment_id(self) -> uuid.UUID | None:
        """Backwards-compat scalar segment identifier.

        The current schema allows a strand to be linked to one or more segments.
        Older callers/tests expect a single `segment_id` attribute.
        """
        if self.segments:
            return self.segments[0].id
        return None


class FiberSpliceClosure(Base):
    __tablename__ = "fiber_splice_closures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    splices = relationship("FiberSplice", back_populates="closure")
    trays = relationship("FiberSpliceTray", back_populates="closure")


class FiberAccessPoint(Base):
    """Fiber Access Point (NAP/FAP) for customer drop connections."""

    __tablename__ = "fiber_access_points"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    access_point_type: Mapped[str | None] = mapped_column(String(60))
    placement: Mapped[str | None] = mapped_column(String(60))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    street: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    county: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class FiberSpliceTray(Base):
    __tablename__ = "fiber_splice_trays"
    __table_args__ = (
        UniqueConstraint(
            "closure_id", "tray_number", name="uq_fiber_splice_trays_closure_tray"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    closure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_closures.id"), nullable=False
    )
    tray_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(160))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    closure = relationship("FiberSpliceClosure", back_populates="trays")
    splices = relationship("FiberSplice", back_populates="tray")


class FiberSplice(Base):
    __tablename__ = "fiber_splices"
    __table_args__ = (
        UniqueConstraint("from_strand_id", "to_strand_id", name="uq_fiber_splices_from_to"),
        UniqueConstraint("tray_id", "position", name="uq_fiber_splices_tray_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    closure_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_closures.id"), nullable=True
    )
    from_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id"), nullable=True
    )
    to_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id"), nullable=True
    )
    tray_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_trays.id")
    )
    position: Mapped[int | None] = mapped_column(Integer)
    splice_type: Mapped[str | None] = mapped_column(String(80))
    loss_db: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    closure = relationship("FiberSpliceClosure", back_populates="splices")
    tray = relationship("FiberSpliceTray", back_populates="splices")
    from_strand = relationship("FiberStrand", back_populates="splices_from", foreign_keys=[from_strand_id])
    to_strand = relationship("FiberStrand", back_populates="splices_to", foreign_keys=[to_strand_id])


class FiberTerminationPoint(Base):
    __tablename__ = "fiber_termination_points"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str | None] = mapped_column(String(160))
    endpoint_type: Mapped[ODNEndpointType] = mapped_column(
        Enum(ODNEndpointType), default=ODNEndpointType.other
    )
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    segments_from = relationship(
        "FiberSegment", back_populates="from_point", foreign_keys="FiberSegment.from_point_id"
    )
    segments_to = relationship(
        "FiberSegment", back_populates="to_point", foreign_keys="FiberSegment.to_point_id"
    )


class FiberCableType(enum.Enum):
    """Types of fiber optic cables."""
    single_mode = "single_mode"
    multi_mode = "multi_mode"
    armored = "armored"
    aerial = "aerial"
    underground = "underground"
    direct_buried = "direct_buried"


class FiberSegment(Base):
    __tablename__ = "fiber_segments"
    __table_args__ = (
        UniqueConstraint("name", name="uq_fiber_segments_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    segment_type: Mapped[FiberSegmentType] = mapped_column(
        Enum(FiberSegmentType), default=FiberSegmentType.distribution
    )
    cable_type: Mapped[FiberCableType | None] = mapped_column(
        Enum(FiberCableType), nullable=True
    )
    fiber_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="Number of fiber cores in the cable")
    from_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_termination_points.id")
    )
    to_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_termination_points.id")
    )
    fiber_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id")
    )
    length_m: Mapped[float | None] = mapped_column(Float)
    route_geom = mapped_column(Geometry("LINESTRING", srid=4326), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    from_point = relationship(
        "FiberTerminationPoint",
        foreign_keys=[from_point_id],
        back_populates="segments_from",
    )
    to_point = relationship(
        "FiberTerminationPoint",
        foreign_keys=[to_point_id],
        back_populates="segments_to",
    )
    fiber_strand = relationship("FiberStrand", back_populates="segments")


class PonPortSplitterLink(Base):
    __tablename__ = "pon_port_splitter_links"
    __table_args__ = (
        UniqueConstraint("pon_port_id", name="uq_pon_port_splitter_links_pon_port"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pon_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id"), nullable=False
    )
    splitter_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitter_ports.id"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    pon_port = relationship("PonPort", back_populates="splitter_link")
    splitter_port = relationship("SplitterPort", back_populates="pon_links")


class OnuType(Base):
    """Hardware catalog entry for ONU/ONT device types."""

    __tablename__ = "onu_types"
    __table_args__ = (
        UniqueConstraint("name", name="uq_onu_types_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    pon_type: Mapped[PonType] = mapped_column(
        Enum(PonType, name="pontype", create_constraint=False),
        nullable=False,
        default=PonType.gpon,
    )
    gpon_channel: Mapped[GponChannel] = mapped_column(
        Enum(GponChannel, name="gponchannel", create_constraint=False),
        nullable=False,
        default=GponChannel.gpon,
    )
    ethernet_ports: Mapped[int] = mapped_column(Integer, default=0)
    wifi_ports: Mapped[int] = mapped_column(Integer, default=0)
    voip_ports: Mapped[int] = mapped_column(Integer, default=0)
    catv_ports: Mapped[int] = mapped_column(Integer, default=0)
    allow_custom_profiles: Mapped[bool] = mapped_column(Boolean, default=True)
    capability: Mapped[OnuCapability] = mapped_column(
        Enum(OnuCapability, name="onucapability", create_constraint=False),
        nullable=False,
        default=OnuCapability.bridging_routing,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    ont_units = relationship("OntUnit", back_populates="onu_type")


class SpeedProfile(Base):
    """OLT-level speed profile catalog entry (download or upload)."""

    __tablename__ = "speed_profiles"
    __table_args__ = (
        UniqueConstraint("name", "direction", name="uq_speed_profiles_name_direction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[SpeedProfileDirection] = mapped_column(
        Enum(SpeedProfileDirection, name="speedprofiledirection", create_constraint=False),
        nullable=False,
    )
    speed_kbps: Mapped[int] = mapped_column(Integer, nullable=False)
    speed_type: Mapped[SpeedProfileType] = mapped_column(
        Enum(SpeedProfileType, name="speedprofiletype", create_constraint=False),
        nullable=False,
        default=SpeedProfileType.internet,
    )
    use_prefix_suffix: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
