import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Float,
    String,
    Text,
    UniqueConstraint,
    text,
)
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property

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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    subscriber = relationship("Subscriber", back_populates="cpe_devices")
    subscription = relationship("Subscription", back_populates="cpe_devices")
    service_address = relationship("Address")
    ports = relationship("Port", back_populates="device")


class Port(Base):
    __tablename__ = "ports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cpe_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    port_type: Mapped[PortType] = mapped_column(
        Enum(PortType), default=PortType.ethernet
    )
    status: Mapped[PortStatus] = mapped_column(
        Enum(PortStatus), default=PortStatus.down
    )
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    device = relationship("CPEDevice", back_populates="ports")
    vlans = relationship("PortVlan", back_populates="port")

    @hybrid_property
    def is_active(self) -> bool:
        return self.status != PortStatus.disabled


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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    pon_ports = relationship("PonPort", back_populates="olt")
    power_units = relationship("OltPowerUnit", back_populates="olt")
    shelves = relationship("OltShelf", back_populates="olt")


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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    olt = relationship("OLTDevice", back_populates="pon_ports")
    olt_card_port = relationship("OltCardPort", back_populates="pon_port")
    ont_assignments = relationship("OntAssignment", back_populates="pon_port")
    splitter_link = relationship(
        "PonPortSplitterLink", back_populates="pon_port", uselist=False
    )


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
    status: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    assignments = relationship("OntAssignment", back_populates="ont_unit")


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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    ont_unit = relationship("OntUnit", back_populates="assignments")
    pon_port = relationship("PonPort", back_populates="ont_assignments")
    subscriber = relationship("Subscriber", back_populates="ont_assignments")
    subscription = relationship("Subscription", back_populates="ont_assignments")
    service_address = relationship("Address")


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
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    splitters = relationship("Splitter", back_populates="fdh")
    region = relationship("RegionZone")


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
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    fdh = relationship("FdhCabinet", back_populates="splitters")
    ports = relationship("SplitterPort", back_populates="splitter")


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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    splices_from = relationship(
        "FiberSplice", back_populates="from_strand", foreign_keys="FiberSplice.from_strand_id"
    )
    splices_to = relationship(
        "FiberSplice", back_populates="to_strand", foreign_keys="FiberSplice.to_strand_id"
    )
    segments = relationship("FiberSegment", back_populates="fiber_strand")


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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    closure = relationship("FiberSpliceClosure", back_populates="trays")
    splices = relationship("FiberSplice", back_populates="tray")


class FiberSplice(Base):
    __tablename__ = "fiber_splices"
    __table_args__ = (
        UniqueConstraint("from_strand_id", "to_strand_id", name="uq_fiber_splices_from_to"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    closure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_closures.id"), nullable=False
    )
    from_strand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id"), nullable=False
    )
    to_strand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id"), nullable=False
    )
    tray_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_trays.id")
    )
    splice_type: Mapped[str | None] = mapped_column(String(80))
    loss_db: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    pon_port = relationship("PonPort", back_populates="splitter_link")
    splitter_port = relationship("SplitterPort", back_populates="pon_links")
