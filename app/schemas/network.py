from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.network import (
    DeviceStatus,
    DeviceType,
    FiberEndpointType,
    FiberSegmentType,
    FiberStrandStatus,
    HardwareUnitStatus,
    IPVersion,
    ODNEndpointType,
    OltPortType,
    PortStatus,
    PortType,
    SplitterPortType,
)


class CPEDeviceBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    device_type: DeviceType = DeviceType.ont
    status: DeviceStatus = DeviceStatus.active
    serial_number: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    mac_address: str | None = Field(default=None, max_length=64)
    installed_at: datetime | None = None
    notes: str | None = None


class CPEDeviceCreate(CPEDeviceBase):
    pass


class CPEDeviceUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    device_type: DeviceType | None = None
    status: DeviceStatus | None = None
    serial_number: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    mac_address: str | None = Field(default=None, max_length=64)
    installed_at: datetime | None = None
    notes: str | None = None


class CPEDeviceRead(CPEDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PortFields(BaseModel):
    olt_id: UUID | None = None
    port_number: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=80)
    port_type: PortType = PortType.ethernet
    status: PortStatus = PortStatus.down
    notes: str | None = None


class PortBase(PortFields):
    device_id: UUID


class PortCreate(PortFields):
    model_config = ConfigDict(extra="forbid")
    device_id: UUID | None = None

    @model_validator(mode="after")
    def _resolve_device(self) -> PortCreate:
        if not self.device_id and not self.olt_id:
            raise ValueError("device_id or olt_id is required.")
        if not self.device_id and self.olt_id:
            self.device_id = self.olt_id
        return self


class PortUpdate(BaseModel):
    device_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)
    port_type: PortType | None = None
    status: PortStatus | None = None
    notes: str | None = None


class PortRead(PortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class VlanBase(BaseModel):
    region_id: UUID
    tag: int = Field(ge=1, le=4094)
    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    is_active: bool = True


class VlanCreate(VlanBase):
    pass


class VlanUpdate(BaseModel):
    region_id: UUID | None = None
    tag: int | None = Field(default=None, ge=1, le=4094)
    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    is_active: bool | None = None


class VlanRead(VlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PortVlanBase(BaseModel):
    port_id: UUID
    vlan_id: UUID
    is_tagged: bool = True


class PortVlanCreate(PortVlanBase):
    pass


class PortVlanUpdate(BaseModel):
    port_id: UUID | None = None
    vlan_id: UUID | None = None
    is_tagged: bool | None = None


class PortVlanRead(PortVlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class IPAssignmentBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    subscription_add_on_id: UUID | None = None
    service_address_id: UUID | None = None
    ip_version: IPVersion = IPVersion.ipv4
    ipv4_address_id: UUID | None = None
    ipv6_address_id: UUID | None = None
    prefix_length: int | None = Field(default=None, ge=0, le=128)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool = True


class IPAssignmentCreate(IPAssignmentBase):
    @model_validator(mode="after")
    def _validate_ip_version(self) -> IPAssignmentCreate:
        if self.ip_version == IPVersion.ipv4:
            if not self.ipv4_address_id or self.ipv6_address_id is not None:
                raise ValueError("ipv4 assignments require ipv4_address_id only.")
        elif self.ip_version == IPVersion.ipv6:
            if not self.ipv6_address_id or self.ipv4_address_id is not None:
                raise ValueError("ipv6 assignments require ipv6_address_id only.")
        return self


class IPAssignmentUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    subscription_add_on_id: UUID | None = None
    service_address_id: UUID | None = None
    ip_version: IPVersion | None = None
    ipv4_address_id: UUID | None = None
    ipv6_address_id: UUID | None = None
    prefix_length: int | None = Field(default=None, ge=0, le=128)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_ip_version(self) -> IPAssignmentUpdate:
        fields_set = self.model_fields_set
        if {"ip_version", "ipv4_address_id", "ipv6_address_id"} & fields_set:
            if self.ip_version is None:
                if self.ipv4_address_id is not None and self.ipv6_address_id is not None:
                    raise ValueError("Provide only one of ipv4_address_id or ipv6_address_id.")
                return self
            if self.ip_version == IPVersion.ipv4:
                if not self.ipv4_address_id or self.ipv6_address_id is not None:
                    raise ValueError("ipv4 assignments require ipv4_address_id only.")
            elif self.ip_version == IPVersion.ipv6:
                if not self.ipv6_address_id or self.ipv4_address_id is not None:
                    raise ValueError("ipv6 assignments require ipv6_address_id only.")
        return self


class IPAssignmentRead(IPAssignmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IpPoolBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    ip_version: IPVersion = IPVersion.ipv4
    cidr: str = Field(min_length=1, max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool = True
    notes: str | None = None


class IpPoolCreate(IpPoolBase):
    pass


class IpPoolUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    ip_version: IPVersion | None = None
    cidr: str | None = Field(default=None, min_length=1, max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    notes: str | None = None


class IpPoolRead(IpPoolBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IpBlockBase(BaseModel):
    pool_id: UUID
    cidr: str = Field(min_length=1, max_length=64)
    is_active: bool = True
    notes: str | None = None


class IpBlockCreate(IpBlockBase):
    pass


class IpBlockUpdate(BaseModel):
    pool_id: UUID | None = None
    cidr: str | None = Field(default=None, min_length=1, max_length=64)
    is_active: bool | None = None
    notes: str | None = None


class IpBlockRead(IpBlockBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IPv4AddressBase(BaseModel):
    address: str = Field(min_length=1, max_length=15)
    pool_id: UUID | None = None
    is_reserved: bool = False
    notes: str | None = None


class IPv4AddressCreate(IPv4AddressBase):
    pass


class IPv4AddressUpdate(BaseModel):
    address: str | None = Field(default=None, min_length=1, max_length=15)
    pool_id: UUID | None = None
    is_reserved: bool | None = None
    notes: str | None = None


class IPv4AddressRead(IPv4AddressBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IPv6AddressBase(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    pool_id: UUID | None = None
    is_reserved: bool = False
    notes: str | None = None


class IPv6AddressCreate(IPv6AddressBase):
    pass


class IPv6AddressUpdate(BaseModel):
    address: str | None = Field(default=None, min_length=1, max_length=64)
    pool_id: UUID | None = None
    is_reserved: bool | None = None
    notes: str | None = None


class IPv6AddressRead(IPv6AddressBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OLTDeviceBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True


class OLTDeviceCreate(OLTDeviceBase):
    pass


class OLTDeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None


class OLTDeviceRead(OLTDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PonPortFields(BaseModel):
    olt_id: UUID
    olt_card_port_id: UUID | None = None
    port_number: int | None = None
    notes: str | None = None
    is_active: bool = True


class PonPortBase(PonPortFields):
    name: str = Field(min_length=1, max_length=120)


class PonPortCreate(PonPortFields):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    card_id: UUID | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _ensure_name(self) -> PonPortCreate:
        if not self.name:
            if self.port_number is None:
                raise ValueError("name or port_number is required.")
            self.name = f"pon-{self.port_number}"
        return self


class PonPortUpdate(BaseModel):
    olt_id: UUID | None = None
    olt_card_port_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    port_number: int | None = None
    notes: str | None = None
    is_active: bool | None = None


class PonPortRead(PonPortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OntUnitBase(BaseModel):
    serial_number: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True


class OntUnitCreate(OntUnitBase):
    pass


class OntUnitUpdate(BaseModel):
    serial_number: str | None = Field(default=None, min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None


class OntUnitRead(OntUnitBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OntAssignmentBase(BaseModel):
    ont_unit_id: UUID
    pon_port_id: UUID
    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool = True
    notes: str | None = None


class OntAssignmentCreate(OntAssignmentBase):
    pass


class OntAssignmentUpdate(BaseModel):
    ont_unit_id: UUID | None = None
    pon_port_id: UUID | None = None
    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool | None = None
    notes: str | None = None


class OntAssignmentRead(OntAssignmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltShelfBase(BaseModel):
    olt_id: UUID
    shelf_number: int = Field(ge=1)
    label: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True


class OltShelfCreate(OltShelfBase):
    pass


class OltShelfUpdate(BaseModel):
    olt_id: UUID | None = None
    shelf_number: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None


class OltShelfRead(OltShelfBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltCardBase(BaseModel):
    shelf_id: UUID
    slot_number: int = Field(ge=1)
    card_type: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True


class OltCardCreate(OltCardBase):
    pass


class OltCardUpdate(BaseModel):
    shelf_id: UUID | None = None
    slot_number: int | None = Field(default=None, ge=1)
    card_type: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None


class OltCardRead(OltCardBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltCardPortBase(BaseModel):
    card_id: UUID
    port_number: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=120)
    port_type: OltPortType = OltPortType.pon
    is_active: bool = True
    notes: str | None = None


class OltCardPortCreate(OltCardPortBase):
    pass


class OltCardPortUpdate(BaseModel):
    card_id: UUID | None = None
    port_number: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, max_length=120)
    port_type: OltPortType | None = None
    is_active: bool | None = None
    notes: str | None = None


class OltCardPortRead(OltCardPortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FdhCabinetBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    region_id: UUID | None = None
    notes: str | None = None
    is_active: bool = True


class FdhCabinetCreate(FdhCabinetBase):
    pass


class FdhCabinetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    region_id: UUID | None = None
    notes: str | None = None
    is_active: bool | None = None


class FdhCabinetRead(FdhCabinetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SplitterBase(BaseModel):
    fdh_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    splitter_ratio: str | None = Field(default=None, max_length=40)
    input_ports: int = Field(default=1, ge=1)
    output_ports: int = Field(default=8, ge=1)
    notes: str | None = None
    is_active: bool = True


class SplitterCreate(SplitterBase):
    pass


class SplitterUpdate(BaseModel):
    fdh_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    splitter_ratio: str | None = Field(default=None, max_length=40)
    input_ports: int | None = Field(default=None, ge=1)
    output_ports: int | None = Field(default=None, ge=1)
    notes: str | None = None
    is_active: bool | None = None


class SplitterRead(SplitterBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SplitterPortBase(BaseModel):
    splitter_id: UUID
    port_number: int = Field(ge=1)
    port_type: SplitterPortType = SplitterPortType.output
    is_active: bool = True
    notes: str | None = None


class SplitterPortCreate(SplitterPortBase):
    pass


class SplitterPortUpdate(BaseModel):
    splitter_id: UUID | None = None
    port_number: int | None = Field(default=None, ge=1)
    port_type: SplitterPortType | None = None
    is_active: bool | None = None
    notes: str | None = None


class SplitterPortRead(SplitterPortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SplitterPortAssignmentBase(BaseModel):
    splitter_port_id: UUID
    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool = True
    notes: str | None = None


class SplitterPortAssignmentCreate(SplitterPortAssignmentBase):
    pass


class SplitterPortAssignmentUpdate(BaseModel):
    splitter_port_id: UUID | None = None
    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool | None = None
    notes: str | None = None


class SplitterPortAssignmentRead(SplitterPortAssignmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberStrandFields(BaseModel):
    strand_number: int = Field(ge=1)
    label: str | None = Field(default=None, max_length=160)
    status: FiberStrandStatus = FiberStrandStatus.available
    upstream_type: FiberEndpointType | None = None
    upstream_id: UUID | None = None
    downstream_type: FiberEndpointType | None = None
    downstream_id: UUID | None = None
    notes: str | None = None
    is_active: bool = True


class FiberStrandBase(FiberStrandFields):
    cable_name: str = Field(min_length=1, max_length=160)


class FiberStrandCreate(FiberStrandFields):
    model_config = ConfigDict(extra="forbid")
    cable_name: str | None = Field(default=None, min_length=1, max_length=160)
    segment_id: UUID | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _resolve_cable_name(self) -> FiberStrandCreate:
        if not self.cable_name:
            if not self.segment_id:
                raise ValueError("cable_name or segment_id is required.")
            self.cable_name = f"segment-{self.segment_id}"
        return self


class FiberStrandUpdate(BaseModel):
    cable_name: str | None = Field(default=None, min_length=1, max_length=160)
    strand_number: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, max_length=160)
    status: FiberStrandStatus | None = None
    upstream_type: FiberEndpointType | None = None
    upstream_id: UUID | None = None
    downstream_type: FiberEndpointType | None = None
    downstream_id: UUID | None = None
    notes: str | None = None
    is_active: bool | None = None


class FiberStrandRead(FiberStrandBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberSpliceClosureBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    notes: str | None = None
    is_active: bool = True


class FiberSpliceClosureCreate(FiberSpliceClosureBase):
    pass


class FiberSpliceClosureUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = None
    is_active: bool | None = None


class FiberSpliceClosureRead(FiberSpliceClosureBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberSpliceTrayBase(BaseModel):
    closure_id: UUID
    tray_number: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=160)
    notes: str | None = None


class FiberSpliceTrayCreate(FiberSpliceTrayBase):
    pass


class FiberSpliceTrayUpdate(BaseModel):
    closure_id: UUID | None = None
    tray_number: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, max_length=160)
    notes: str | None = None


class FiberSpliceTrayRead(FiberSpliceTrayBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberSpliceFields(BaseModel):
    tray_id: UUID | None = None
    splice_type: str | None = Field(default=None, max_length=80)
    loss_db: float | None = None
    notes: str | None = None


class FiberSpliceBase(FiberSpliceFields):
    closure_id: UUID
    from_strand_id: UUID
    to_strand_id: UUID


class FiberSpliceCreate(FiberSpliceFields):
    model_config = ConfigDict(extra="forbid")
    closure_id: UUID | None = None
    from_strand_id: UUID | None = None
    to_strand_id: UUID | None = None
    position: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_requirements(self) -> FiberSpliceCreate:
        has_full = self.closure_id and self.from_strand_id and self.to_strand_id
        has_tray = self.tray_id and self.position
        if not has_full and not has_tray:
            raise ValueError(
                "Provide closure_id/from_strand_id/to_strand_id or tray_id/position."
            )
        return self


class FiberSpliceUpdate(BaseModel):
    closure_id: UUID | None = None
    from_strand_id: UUID | None = None
    to_strand_id: UUID | None = None
    tray_id: UUID | None = None
    position: int | None = Field(default=None, ge=1)
    splice_type: str | None = Field(default=None, max_length=80)
    loss_db: float | None = None
    notes: str | None = None


class FiberSpliceRead(FiberSpliceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class FiberTerminationPointBase(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    endpoint_type: ODNEndpointType = ODNEndpointType.other
    ref_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool = True


class FiberTerminationPointCreate(FiberTerminationPointBase):
    pass


class FiberTerminationPointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    endpoint_type: ODNEndpointType | None = None
    ref_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool | None = None


class FiberTerminationPointRead(FiberTerminationPointBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberSegmentBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    segment_type: FiberSegmentType = FiberSegmentType.distribution
    from_point_id: UUID | None = None
    to_point_id: UUID | None = None
    fiber_strand_id: UUID | None = None
    length_m: float | None = None
    notes: str | None = None
    is_active: bool = True


class FiberSegmentCreate(FiberSegmentBase):
    pass


class FiberSegmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    segment_type: FiberSegmentType | None = None
    from_point_id: UUID | None = None
    to_point_id: UUID | None = None
    fiber_strand_id: UUID | None = None
    length_m: float | None = None
    notes: str | None = None
    is_active: bool | None = None


class FiberSegmentRead(FiberSegmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PonPortSplitterLinkBase(BaseModel):
    pon_port_id: UUID
    splitter_port_id: UUID
    active: bool = True
    notes: str | None = None


class PonPortSplitterLinkCreate(PonPortSplitterLinkBase):
    pass


class PonPortSplitterLinkUpdate(BaseModel):
    pon_port_id: UUID | None = None
    splitter_port_id: UUID | None = None
    active: bool | None = None
    notes: str | None = None


class PonPortSplitterLinkRead(PonPortSplitterLinkBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltPowerUnitBase(BaseModel):
    olt_id: UUID
    slot: str = Field(min_length=1, max_length=40)
    status: HardwareUnitStatus | None = None
    notes: str | None = None
    is_active: bool = True


class OltPowerUnitCreate(OltPowerUnitBase):
    pass


class OltPowerUnitUpdate(BaseModel):
    olt_id: UUID | None = None
    slot: str | None = Field(default=None, min_length=1, max_length=40)
    status: HardwareUnitStatus | None = None
    notes: str | None = None
    is_active: bool | None = None


class OltPowerUnitRead(OltPowerUnitBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltSfpModuleBase(BaseModel):
    olt_card_port_id: UUID
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    wavelength_nm: int | None = None
    rx_power_dbm: float | None = None
    tx_power_dbm: float | None = None
    installed_at: datetime | None = None
    is_active: bool = True
    notes: str | None = None


class OltSfpModuleCreate(OltSfpModuleBase):
    pass


class OltSfpModuleUpdate(BaseModel):
    olt_card_port_id: UUID | None = None
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    wavelength_nm: int | None = None
    rx_power_dbm: float | None = None
    tx_power_dbm: float | None = None
    installed_at: datetime | None = None
    is_active: bool | None = None
    notes: str | None = None


class OltSfpModuleRead(OltSfpModuleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
