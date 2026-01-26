from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    DeviceType,
    IPAssignment,
    IPVersion,
    IpBlock,
    IpPool,
    IPv4Address,
    IPv6Address,
    FiberSegment,
    FiberSegmentType,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FdhCabinet,
    FiberTerminationPoint,
    OLTDevice,
    OltPortType,
    OltPowerUnit,
    OltSfpModule,
    OltCard,
    OltCardPort,
    OltShelf,
    FiberStrandStatus,
    PortStatus,
    PortType,
    SplitterPortType,
    FiberEndpointType,
    ODNEndpointType,
    OntAssignment,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Port,
    PortVlan,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
    Vlan,
)
from app.models.person import Person
import uuid
from app.models.subscriber import Subscriber, SubscriberAccount
from app.models.domain_settings import SettingDomain
from app.services.response import ListResponseMixin
from app.services import settings_spec
from app.schemas.network import (
    CPEDeviceCreate,
    CPEDeviceUpdate,
    IPAssignmentCreate,
    IPAssignmentUpdate,
    IpBlockCreate,
    IpBlockUpdate,
    IpPoolCreate,
    IpPoolUpdate,
    FiberSegmentCreate,
    FiberSegmentUpdate,
    FiberSpliceClosureCreate,
    FiberSpliceClosureUpdate,
    FiberSpliceCreate,
    FiberSpliceUpdate,
    FiberSpliceTrayCreate,
    FiberSpliceTrayUpdate,
    FiberStrandCreate,
    FiberStrandUpdate,
    FiberTerminationPointCreate,
    FiberTerminationPointUpdate,
    FdhCabinetCreate,
    FdhCabinetUpdate,
    IPv4AddressCreate,
    IPv4AddressUpdate,
    IPv6AddressCreate,
    IPv6AddressUpdate,
    OLTDeviceCreate,
    OLTDeviceUpdate,
    OltPowerUnitCreate,
    OltPowerUnitUpdate,
    OltSfpModuleCreate,
    OltSfpModuleUpdate,
    OltCardCreate,
    OltCardPortCreate,
    OltCardPortUpdate,
    OltCardUpdate,
    OltShelfCreate,
    OltShelfUpdate,
    OntAssignmentCreate,
    OntAssignmentUpdate,
    OntUnitCreate,
    OntUnitUpdate,
    PonPortSplitterLinkCreate,
    PonPortSplitterLinkUpdate,
    PonPortCreate,
    PonPortUpdate,
    PortCreate,
    PortUpdate,
    PortVlanCreate,
    PortVlanUpdate,
    SplitterCreate,
    SplitterPortAssignmentCreate,
    SplitterPortAssignmentUpdate,
    SplitterPortCreate,
    SplitterPortUpdate,
    SplitterUpdate,
    VlanCreate,
    VlanUpdate,
)
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.validators import network as network_validators


class CPEDevices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CPEDeviceCreate):
        network_validators.validate_cpe_device_links(
            db,
            str(payload.account_id),
            str(payload.subscription_id) if payload.subscription_id else None,
            str(payload.service_address_id) if payload.service_address_id else None,
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "device_type" not in fields_set:
            default_type = settings_spec.resolve_value(
                db, SettingDomain.network, "default_device_type"
            )
            if default_type:
                data["device_type"] = validate_enum(
                    default_type, DeviceType, "device_type"
                )
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.network, "default_device_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, DeviceStatus, "status"
                )
        device = CPEDevice(**data)
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str):
        device = db.get(CPEDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="CPE device not found")
        return device

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        subscription_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CPEDevice)
        if account_id:
            query = query.filter(CPEDevice.account_id == account_id)
        if subscription_id:
            query = query.filter(CPEDevice.subscription_id == subscription_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CPEDevice.created_at, "updated_at": CPEDevice.updated_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, device_id: str, payload: CPEDeviceUpdate):
        device = db.get(CPEDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="CPE device not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = str(data.get("account_id", device.account_id))
        subscription_id = data.get("subscription_id", device.subscription_id)
        service_address_id = data.get("service_address_id", device.service_address_id)
        network_validators.validate_cpe_device_links(
            db,
            account_id,
            str(subscription_id) if subscription_id else None,
            str(service_address_id) if service_address_id else None,
        )
        for key, value in data.items():
            setattr(device, key, value)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str):
        device = db.get(CPEDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="CPE device not found")
        db.delete(device)
        db.commit()


class Ports(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PortCreate):
        data = payload.model_dump(exclude={"olt_id", "port_number"})
        if not data.get("device_id") and payload.olt_id:
            data["device_id"] = payload.olt_id
        if data.get("device_id") and not db.get(CPEDevice, data["device_id"]) and payload.olt_id:
            person = Person(
                first_name="OLT",
                last_name="Port",
                email=f"olt-port-{uuid.uuid4()}@example.invalid",
            )
            db.add(person)
            db.flush()
            subscriber = Subscriber(person_id=person.id)
            db.add(subscriber)
            db.flush()
            account = SubscriberAccount(subscriber_id=subscriber.id)
            db.add(account)
            db.flush()
            device = CPEDevice(
                id=data["device_id"],
                account_id=account.id,
                device_type=DeviceType.ont,
            )
            db.add(device)
            db.flush()
        fields_set = payload.model_fields_set
        if "port_type" not in fields_set:
            default_type = settings_spec.resolve_value(
                db, SettingDomain.network, "default_port_type"
            )
            if default_type:
                data["port_type"] = validate_enum(
                    default_type, PortType, "port_type"
                )
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.network, "default_port_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, PortStatus, "status"
                )
        port = Port(**data)
        db.add(port)
        db.commit()
        db.refresh(port)
        port.olt_id = payload.olt_id
        port.port_number = payload.port_number
        return port

    @staticmethod
    def get(db: Session, port_id: str):
        port = db.get(Port, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Port not found")
        return port

    @staticmethod
    def list(
        db: Session,
        device_id: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
        olt_id: str | None = None,
        port_type: str | None = None,
        status: str | None = None,
        is_active: bool | None = None,
    ):
        query = db.query(Port)
        if not device_id and olt_id:
            device_id = olt_id
        if device_id:
            query = query.filter(Port.device_id == device_id)
        if port_type:
            query = query.filter(
                Port.port_type == validate_enum(port_type, PortType, "port_type")
            )
        if status:
            query = query.filter(
                Port.status == validate_enum(status, PortStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Port.created_at, "name": Port.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, port_id: str, payload: PortUpdate):
        port = db.get(Port, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Port not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(port, key, value)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def delete(db: Session, port_id: str):
        port = db.get(Port, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Port not found")
        port.status = PortStatus.disabled
        db.commit()


class Vlans(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: VlanCreate):
        vlan = Vlan(**payload.model_dump())
        db.add(vlan)
        db.commit()
        db.refresh(vlan)
        return vlan

    @staticmethod
    def get(db: Session, vlan_id: str):
        vlan = db.get(Vlan, vlan_id)
        if not vlan:
            raise HTTPException(status_code=404, detail="VLAN not found")
        return vlan

    @staticmethod
    def list(
        db: Session,
        region_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Vlan)
        if region_id:
            query = query.filter(Vlan.region_id == region_id)
        if is_active is None:
            query = query.filter(Vlan.is_active.is_(True))
        else:
            query = query.filter(Vlan.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Vlan.created_at, "tag": Vlan.tag},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, vlan_id: str, payload: VlanUpdate):
        vlan = db.get(Vlan, vlan_id)
        if not vlan:
            raise HTTPException(status_code=404, detail="VLAN not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(vlan, key, value)
        db.commit()
        db.refresh(vlan)
        return vlan

    @staticmethod
    def delete(db: Session, vlan_id: str):
        vlan = db.get(Vlan, vlan_id)
        if not vlan:
            raise HTTPException(status_code=404, detail="VLAN not found")
        vlan.is_active = False
        db.commit()


class PortVlans(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PortVlanCreate):
        link = PortVlan(**payload.model_dump())
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(PortVlan, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Port VLAN link not found")
        return link

    @staticmethod
    def list(
        db: Session,
        port_id: str | None,
        vlan_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PortVlan)
        if port_id:
            query = query.filter(PortVlan.port_id == port_id)
        if vlan_id:
            query = query.filter(PortVlan.vlan_id == vlan_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "port_id": PortVlan.port_id,
                "vlan_id": PortVlan.vlan_id,
                "created_at": PortVlan.created_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, link_id: str, payload: PortVlanUpdate):
        link = db.get(PortVlan, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Port VLAN link not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(link, key, value)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str):
        link = db.get(PortVlan, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Port VLAN link not found")
        db.delete(link)
        db.commit()


class IPAssignments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IPAssignmentCreate):
        network_validators.validate_ip_assignment_links(
            db,
            str(payload.account_id),
            str(payload.subscription_id) if payload.subscription_id else None,
            str(payload.subscription_add_on_id) if payload.subscription_add_on_id else None,
            str(payload.service_address_id) if payload.service_address_id else None,
        )
        assignment = IPAssignment(**payload.model_dump())
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def get(db: Session, assignment_id: str):
        assignment = db.get(IPAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="IP assignment not found")
        return assignment

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        subscription_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IPAssignment)
        if account_id:
            query = query.filter(IPAssignment.account_id == account_id)
        if subscription_id:
            query = query.filter(IPAssignment.subscription_id == subscription_id)
        if is_active is None:
            query = query.filter(IPAssignment.is_active.is_(True))
        else:
            query = query.filter(IPAssignment.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPAssignment.created_at, "ipv4_address_id": IPAssignment.ipv4_address_id},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, assignment_id: str, payload: IPAssignmentUpdate):
        assignment = db.get(IPAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="IP assignment not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = str(data.get("account_id", assignment.account_id))
        subscription_id = data.get("subscription_id", assignment.subscription_id)
        subscription_add_on_id = data.get(
            "subscription_add_on_id", assignment.subscription_add_on_id
        )
        service_address_id = data.get(
            "service_address_id", assignment.service_address_id
        )
        network_validators.validate_ip_assignment_links(
            db,
            account_id,
            str(subscription_id) if subscription_id else None,
            str(subscription_add_on_id) if subscription_add_on_id else None,
            str(service_address_id) if service_address_id else None,
        )
        for key, value in data.items():
            setattr(assignment, key, value)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def delete(db: Session, assignment_id: str):
        assignment = db.get(IPAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="IP assignment not found")
        assignment.is_active = False
        db.commit()


class IpPools(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IpPoolCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "ip_version" not in fields_set:
            default_version = settings_spec.resolve_value(
                db, SettingDomain.network, "default_ip_version"
            )
            if default_version:
                data["ip_version"] = validate_enum(
                    default_version, IPVersion, "ip_version"
                )
        pool = IpPool(**data)
        db.add(pool)
        db.commit()
        db.refresh(pool)
        return pool

    @staticmethod
    def get(db: Session, pool_id: str):
        pool = db.get(IpPool, coerce_uuid(pool_id))
        if not pool:
            raise HTTPException(status_code=404, detail="IP pool not found")
        return pool

    @staticmethod
    def list(
        db: Session,
        ip_version: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IpPool)
        if ip_version:
            query = query.filter(IpPool.ip_version == ip_version)
        if is_active is None:
            query = query.filter(IpPool.is_active.is_(True))
        else:
            query = query.filter(IpPool.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpPool.created_at, "name": IpPool.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, pool_id: str, payload: IpPoolUpdate):
        pool = db.get(IpPool, pool_id)
        if not pool:
            raise HTTPException(status_code=404, detail="IP pool not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(pool, key, value)
        db.commit()
        db.refresh(pool)
        return pool

    @staticmethod
    def delete(db: Session, pool_id: str):
        pool = db.get(IpPool, pool_id)
        if not pool:
            raise HTTPException(status_code=404, detail="IP pool not found")
        pool.is_active = False
        db.commit()


class IpBlocks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IpBlockCreate):
        data = payload.model_dump()
        block = IpBlock(**data)
        db.add(block)
        db.commit()
        db.refresh(block)
        return block

    @staticmethod
    def get(db: Session, block_id: str):
        block = db.get(IpBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="IP block not found")
        return block

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IpBlock)
        if pool_id:
            query = query.filter(IpBlock.pool_id == pool_id)
        if is_active is None:
            query = query.filter(IpBlock.is_active.is_(True))
        else:
            query = query.filter(IpBlock.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpBlock.created_at, "cidr": IpBlock.cidr},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, block_id: str, payload: IpBlockUpdate):
        block = db.get(IpBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="IP block not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(block, key, value)
        db.commit()
        db.refresh(block)
        return block

    @staticmethod
    def delete(db: Session, block_id: str):
        block = db.get(IpBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="IP block not found")
        block.is_active = False
        db.commit()


class IPv4Addresses(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IPv4AddressCreate):
        address = IPv4Address(**payload.model_dump())
        db.add(address)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def get(db: Session, address_id: str):
        address = db.get(IPv4Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv4 address not found")
        return address

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None,
        is_reserved: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IPv4Address)
        if pool_id:
            query = query.filter(IPv4Address.pool_id == pool_id)
        if is_reserved is not None:
            query = query.filter(IPv4Address.is_reserved == is_reserved)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv4Address.created_at, "address": IPv4Address.address},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, address_id: str, payload: IPv4AddressUpdate):
        address = db.get(IPv4Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv4 address not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(address, key, value)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def delete(db: Session, address_id: str):
        address = db.get(IPv4Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv4 address not found")
        db.delete(address)
        db.commit()


class IPv6Addresses(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IPv6AddressCreate):
        address = IPv6Address(**payload.model_dump())
        db.add(address)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def get(db: Session, address_id: str):
        address = db.get(IPv6Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv6 address not found")
        return address

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None,
        is_reserved: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IPv6Address)
        if pool_id:
            query = query.filter(IPv6Address.pool_id == pool_id)
        if is_reserved is not None:
            query = query.filter(IPv6Address.is_reserved == is_reserved)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv6Address.created_at, "address": IPv6Address.address},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, address_id: str, payload: IPv6AddressUpdate):
        address = db.get(IPv6Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv6 address not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(address, key, value)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def delete(db: Session, address_id: str):
        address = db.get(IPv6Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv6 address not found")
        db.delete(address)
        db.commit()


class OLTDevices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OLTDeviceCreate):
        device = OLTDevice(**payload.model_dump())
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str):
        device = db.get(OLTDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="OLT device not found")
        return device

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OLTDevice)
        if is_active is None:
            query = query.filter(OLTDevice.is_active.is_(True))
        else:
            query = query.filter(OLTDevice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OLTDevice.created_at, "name": OLTDevice.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, device_id: str, payload: OLTDeviceUpdate):
        device = db.get(OLTDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="OLT device not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(device, key, value)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str):
        device = db.get(OLTDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="OLT device not found")
        device.is_active = False
        db.commit()


class PonPorts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PonPortCreate):
        olt = db.get(OLTDevice, payload.olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")
        if payload.olt_card_port_id:
            card_port = db.get(OltCardPort, payload.olt_card_port_id)
            if not card_port:
                raise HTTPException(status_code=404, detail="OLT card port not found")
        elif payload.card_id:
            card = db.get(OltCard, payload.card_id)
            if not card:
                raise HTTPException(status_code=404, detail="OLT card not found")
            if card.shelf and str(card.shelf.olt_id) != str(payload.olt_id):
                raise HTTPException(status_code=400, detail="OLT card does not belong to OLT device")
            card_port = (
                db.query(OltCardPort)
                .filter(OltCardPort.card_id == payload.card_id)
                .filter(OltCardPort.port_number == payload.port_number)
                .first()
            )
            if not card_port:
                card_port = OltCardPort(
                    card_id=payload.card_id,
                    port_number=payload.port_number or 1,
                )
                db.add(card_port)
                db.flush()
            payload.olt_card_port_id = card_port.id
        data = payload.model_dump(exclude={"card_id"})
        port = PonPort(**data)
        db.add(port)
        db.commit()
        db.refresh(port)
        port.card_id = payload.card_id
        return port

    @staticmethod
    def get(db: Session, port_id: str):
        port = db.get(PonPort, port_id)
        if not port or not port.is_active:
            raise HTTPException(status_code=404, detail="PON port not found")
        return port

    @staticmethod
    def list(
        db: Session,
        olt_id: str | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
        card_id: str | None = None,
    ):
        query = db.query(PonPort)
        if olt_id:
            query = query.filter(PonPort.olt_id == olt_id)
        if card_id:
            query = query.join(
                OltCardPort, OltCardPort.id == PonPort.olt_card_port_id, isouter=True
            )
            query = query.filter(
                (OltCardPort.card_id == card_id) | (PonPort.olt_card_port_id.is_(None))
            )
        if is_active is None:
            query = query.filter(PonPort.is_active.is_(True))
        else:
            query = query.filter(PonPort.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PonPort.created_at, "name": PonPort.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, port_id: str, payload: PonPortUpdate):
        port = db.get(PonPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="PON port not found")
        data = payload.model_dump(exclude_unset=True)
        if "olt_id" in data:
            olt = db.get(OLTDevice, data["olt_id"])
            if not olt:
                raise HTTPException(status_code=404, detail="OLT device not found")
        if "olt_card_port_id" in data and data["olt_card_port_id"]:
            card_port = db.get(OltCardPort, data["olt_card_port_id"])
            if not card_port:
                raise HTTPException(status_code=404, detail="OLT card port not found")
        for key, value in data.items():
            setattr(port, key, value)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def delete(db: Session, port_id: str):
        port = db.get(PonPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="PON port not found")
        port.is_active = False
        db.commit()

    @staticmethod
    def utilization(db: Session, olt_id: str | None):
        query = db.query(PonPort)
        if olt_id:
            query = query.filter(PonPort.olt_id == olt_id)
        total_ports = query.filter(PonPort.is_active.is_(True)).count()
        assigned_ports = (
            db.query(OntAssignment.pon_port_id)
            .filter(OntAssignment.active.is_(True))
        )
        if olt_id:
            assigned_ports = assigned_ports.filter(
                OntAssignment.pon_port_id.in_(
                    db.query(PonPort.id).filter(PonPort.olt_id == olt_id)
                )
            )
        assigned_count = assigned_ports.distinct().count()
        return {
            "olt_id": olt_id,
            "total_ports": total_ports,
            "assigned_ports": assigned_count,
        }


class OntUnits(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OntUnitCreate):
        unit = OntUnit(**payload.model_dump())
        db.add(unit)
        db.commit()
        db.refresh(unit)
        return unit

    @staticmethod
    def get(db: Session, unit_id: str):
        unit = db.get(OntUnit, unit_id)
        if not unit:
            raise HTTPException(status_code=404, detail="ONT unit not found")
        return unit

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OntUnit)
        if is_active is None:
            query = query.filter(OntUnit.is_active.is_(True))
        else:
            query = query.filter(OntUnit.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OntUnit.created_at, "serial_number": OntUnit.serial_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, unit_id: str, payload: OntUnitUpdate):
        unit = db.get(OntUnit, unit_id)
        if not unit:
            raise HTTPException(status_code=404, detail="ONT unit not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(unit, key, value)
        db.commit()
        db.refresh(unit)
        return unit

    @staticmethod
    def delete(db: Session, unit_id: str):
        unit = db.get(OntUnit, unit_id)
        if not unit:
            raise HTTPException(status_code=404, detail="ONT unit not found")
        unit.is_active = False
        db.commit()


class OntAssignments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OntAssignmentCreate):
        assignment = OntAssignment(**payload.model_dump())
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def get(db: Session, assignment_id: str):
        assignment = db.get(OntAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="ONT assignment not found")
        return assignment

    @staticmethod
    def list(
        db: Session,
        ont_unit_id: str | None,
        pon_port_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OntAssignment)
        if ont_unit_id:
            query = query.filter(OntAssignment.ont_unit_id == ont_unit_id)
        if pon_port_id:
            query = query.filter(OntAssignment.pon_port_id == pon_port_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OntAssignment.created_at, "active": OntAssignment.active},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, assignment_id: str, payload: OntAssignmentUpdate):
        assignment = db.get(OntAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="ONT assignment not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(assignment, key, value)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def delete(db: Session, assignment_id: str):
        assignment = db.get(OntAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="ONT assignment not found")
        db.delete(assignment)
        db.commit()


class OltShelves(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OltShelfCreate):
        olt = db.get(OLTDevice, payload.olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")
        shelf = OltShelf(**payload.model_dump())
        db.add(shelf)
        db.commit()
        db.refresh(shelf)
        return shelf

    @staticmethod
    def get(db: Session, shelf_id: str):
        shelf = db.get(OltShelf, shelf_id)
        if not shelf:
            raise HTTPException(status_code=404, detail="OLT shelf not found")
        return shelf

    @staticmethod
    def list(
        db: Session,
        olt_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OltShelf)
        if olt_id:
            query = query.filter(OltShelf.olt_id == olt_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltShelf.created_at, "shelf_number": OltShelf.shelf_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, shelf_id: str, payload: OltShelfUpdate):
        shelf = db.get(OltShelf, shelf_id)
        if not shelf:
            raise HTTPException(status_code=404, detail="OLT shelf not found")
        data = payload.model_dump(exclude_unset=True)
        if "olt_id" in data:
            olt = db.get(OLTDevice, data["olt_id"])
            if not olt:
                raise HTTPException(status_code=404, detail="OLT device not found")
        for key, value in data.items():
            setattr(shelf, key, value)
        db.commit()
        db.refresh(shelf)
        return shelf

    @staticmethod
    def delete(db: Session, shelf_id: str):
        shelf = db.get(OltShelf, shelf_id)
        if not shelf:
            raise HTTPException(status_code=404, detail="OLT shelf not found")
        db.delete(shelf)
        db.commit()


class OltCards(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OltCardCreate):
        shelf = db.get(OltShelf, payload.shelf_id)
        if not shelf:
            raise HTTPException(status_code=404, detail="OLT shelf not found")
        card = OltCard(**payload.model_dump())
        db.add(card)
        db.commit()
        db.refresh(card)
        return card

    @staticmethod
    def get(db: Session, card_id: str):
        card = db.get(OltCard, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="OLT card not found")
        return card

    @staticmethod
    def list(
        db: Session,
        shelf_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OltCard)
        if shelf_id:
            query = query.filter(OltCard.shelf_id == shelf_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltCard.created_at, "slot_number": OltCard.slot_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, card_id: str, payload: OltCardUpdate):
        card = db.get(OltCard, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="OLT card not found")
        data = payload.model_dump(exclude_unset=True)
        if "shelf_id" in data:
            shelf = db.get(OltShelf, data["shelf_id"])
            if not shelf:
                raise HTTPException(status_code=404, detail="OLT shelf not found")
        for key, value in data.items():
            setattr(card, key, value)
        db.commit()
        db.refresh(card)
        return card

    @staticmethod
    def delete(db: Session, card_id: str):
        card = db.get(OltCard, card_id)
        if not card:
            raise HTTPException(status_code=404, detail="OLT card not found")
        db.delete(card)
        db.commit()


class OltCardPorts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OltCardPortCreate):
        card = db.get(OltCard, payload.card_id)
        if not card:
            raise HTTPException(status_code=404, detail="OLT card not found")
        port = OltCardPort(**payload.model_dump())
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def get(db: Session, port_id: str):
        port = db.get(OltCardPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="OLT card port not found")
        return port

    @staticmethod
    def list(
        db: Session,
        card_id: str | None = None,
        port_type: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ):
        query = db.query(OltCardPort)
        if card_id:
            query = query.filter(OltCardPort.card_id == card_id)
        if port_type:
            query = query.filter(
                OltCardPort.port_type == validate_enum(port_type, OltPortType, "port_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltCardPort.created_at, "port_number": OltCardPort.port_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, port_id: str, payload: OltCardPortUpdate):
        port = db.get(OltCardPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="OLT card port not found")
        data = payload.model_dump(exclude_unset=True)
        if "card_id" in data:
            card = db.get(OltCard, data["card_id"])
            if not card:
                raise HTTPException(status_code=404, detail="OLT card not found")
        for key, value in data.items():
            setattr(port, key, value)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def delete(db: Session, port_id: str):
        port = db.get(OltCardPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="OLT card port not found")
        db.delete(port)
        db.commit()


class FdhCabinets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FdhCabinetCreate):
        cabinet = FdhCabinet(**payload.model_dump())
        db.add(cabinet)
        db.commit()
        db.refresh(cabinet)
        return cabinet

    @staticmethod
    def get(db: Session, cabinet_id: str):
        cabinet = db.get(FdhCabinet, cabinet_id)
        if not cabinet:
            raise HTTPException(status_code=404, detail="FDH cabinet not found")
        return cabinet

    @staticmethod
    def list(
        db: Session,
        region_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FdhCabinet)
        if region_id:
            query = query.filter(FdhCabinet.region_id == region_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FdhCabinet.created_at, "name": FdhCabinet.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, cabinet_id: str, payload: FdhCabinetUpdate):
        cabinet = db.get(FdhCabinet, cabinet_id)
        if not cabinet:
            raise HTTPException(status_code=404, detail="FDH cabinet not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(cabinet, key, value)
        db.commit()
        db.refresh(cabinet)
        return cabinet

    @staticmethod
    def delete(db: Session, cabinet_id: str):
        cabinet = db.get(FdhCabinet, cabinet_id)
        if not cabinet:
            raise HTTPException(status_code=404, detail="FDH cabinet not found")
        db.delete(cabinet)
        db.commit()


class Splitters(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SplitterCreate):
        if payload.fdh_id:
            cabinet = db.get(FdhCabinet, payload.fdh_id)
            if not cabinet:
                raise HTTPException(status_code=404, detail="FDH cabinet not found")
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "input_ports" not in fields_set:
            default_input = settings_spec.resolve_value(
                db, SettingDomain.network, "default_splitter_input_ports"
            )
            if default_input:
                data["input_ports"] = default_input
        if "output_ports" not in fields_set:
            default_output = settings_spec.resolve_value(
                db, SettingDomain.network, "default_splitter_output_ports"
            )
            if default_output:
                data["output_ports"] = default_output
        splitter = Splitter(**data)
        db.add(splitter)
        db.commit()
        db.refresh(splitter)
        return splitter

    @staticmethod
    def get(db: Session, splitter_id: str):
        splitter = db.get(Splitter, splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        return splitter

    @staticmethod
    def list(
        db: Session,
        fdh_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Splitter)
        if fdh_id:
            query = query.filter(Splitter.fdh_id == fdh_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Splitter.created_at, "name": Splitter.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, splitter_id: str, payload: SplitterUpdate):
        splitter = db.get(Splitter, splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        data = payload.model_dump(exclude_unset=True)
        if "fdh_id" in data and data["fdh_id"]:
            cabinet = db.get(FdhCabinet, data["fdh_id"])
            if not cabinet:
                raise HTTPException(status_code=404, detail="FDH cabinet not found")
        for key, value in data.items():
            setattr(splitter, key, value)
        db.commit()
        db.refresh(splitter)
        return splitter

    @staticmethod
    def delete(db: Session, splitter_id: str):
        splitter = db.get(Splitter, splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        db.delete(splitter)
        db.commit()


class SplitterPorts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SplitterPortCreate):
        splitter = db.get(Splitter, payload.splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        port = SplitterPort(**payload.model_dump())
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def get(db: Session, port_id: str):
        port = db.get(SplitterPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Splitter port not found")
        return port

    @staticmethod
    def list(
        db: Session,
        splitter_id: str | None,
        port_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SplitterPort)
        if splitter_id:
            query = query.filter(SplitterPort.splitter_id == splitter_id)
        if port_type:
            query = query.filter(
                SplitterPort.port_type
                == validate_enum(port_type, SplitterPortType, "port_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SplitterPort.created_at, "port_number": SplitterPort.port_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def utilization(db: Session, splitter_id: str):
        splitter = db.get(Splitter, splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        total_ports = (
            db.query(SplitterPort)
            .filter(SplitterPort.splitter_id == splitter_id)
            .filter(SplitterPort.is_active.is_(True))
            .count()
        )
        splitter_ports_subquery = db.query(SplitterPort.id).filter(
            SplitterPort.splitter_id == splitter_id
        )
        fiber_used = (
            db.query(FiberStrand.upstream_id)
            .filter(FiberStrand.upstream_type == FiberEndpointType.splitter_port)
            .filter(FiberStrand.upstream_id.in_(splitter_ports_subquery))
        )
        assigned_used = (
            db.query(SplitterPortAssignment.splitter_port_id)
            .filter(SplitterPortAssignment.active.is_(True))
            .filter(SplitterPortAssignment.splitter_port_id.in_(splitter_ports_subquery))
        )
        used_ports = (
            db.query(SplitterPort.id)
            .filter(SplitterPort.id.in_(fiber_used.union_all(assigned_used)))
            .distinct()
            .count()
        )
        return {"splitter_id": splitter_id, "total_ports": total_ports, "used_ports": used_ports}

    @staticmethod
    def update(db: Session, port_id: str, payload: SplitterPortUpdate):
        port = db.get(SplitterPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Splitter port not found")
        data = payload.model_dump(exclude_unset=True)
        if "splitter_id" in data:
            splitter = db.get(Splitter, data["splitter_id"])
            if not splitter:
                raise HTTPException(status_code=404, detail="Splitter not found")
        for key, value in data.items():
            setattr(port, key, value)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def delete(db: Session, port_id: str):
        port = db.get(SplitterPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Splitter port not found")
        db.delete(port)
        db.commit()


class FiberStrands(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberStrandCreate):
        segment = None
        if payload.segment_id:
            segment = db.get(FiberSegment, payload.segment_id)
            if not segment:
                raise HTTPException(status_code=404, detail="Fiber segment not found")
        data = payload.model_dump(exclude={"segment_id"})
        if segment and (not payload.cable_name or payload.cable_name.startswith("segment-")):
            data["cable_name"] = segment.name
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.network, "default_fiber_strand_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, FiberStrandStatus, "status"
                )
        strand = FiberStrand(**data)
        db.add(strand)
        db.commit()
        db.refresh(strand)
        if segment:
            strand.segment_id = segment.id
        return strand

    @staticmethod
    def get(db: Session, strand_id: str):
        strand = db.get(FiberStrand, strand_id)
        if not strand:
            raise HTTPException(status_code=404, detail="Fiber strand not found")
        return strand

    @staticmethod
    def list(
        db: Session,
        cable_name: str | None = None,
        status: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
        segment_id: str | None = None,
        is_active: bool | None = None,
    ):
        query = db.query(FiberStrand)
        if segment_id:
            segment = db.get(FiberSegment, segment_id)
            if not segment:
                raise HTTPException(status_code=404, detail="Fiber segment not found")
            query = query.filter(FiberStrand.cable_name == segment.name)
        if cable_name:
            query = query.filter(FiberStrand.cable_name == cable_name)
        if status:
            query = query.filter(
                FiberStrand.status
                == validate_enum(status, FiberStrandStatus, "status")
            )
        if is_active is None:
            query = query.filter(FiberStrand.is_active.is_(True))
        else:
            query = query.filter(FiberStrand.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberStrand.created_at, "strand_number": FiberStrand.strand_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, strand_id: str, payload: FiberStrandUpdate):
        strand = db.get(FiberStrand, strand_id)
        if not strand:
            raise HTTPException(status_code=404, detail="Fiber strand not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(strand, key, value)
        db.commit()
        db.refresh(strand)
        return strand

    @staticmethod
    def delete(db: Session, strand_id: str):
        strand = db.get(FiberStrand, strand_id)
        if not strand:
            raise HTTPException(status_code=404, detail="Fiber strand not found")
        strand.is_active = False
        db.commit()


class FiberSpliceClosures(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSpliceClosureCreate):
        closure = FiberSpliceClosure(**payload.model_dump())
        db.add(closure)
        db.commit()
        db.refresh(closure)
        return closure

    @staticmethod
    def get(db: Session, closure_id: str):
        closure = db.get(FiberSpliceClosure, closure_id)
        if not closure:
            raise HTTPException(status_code=404, detail="Fiber splice closure not found")
        return closure

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberSpliceClosure)
        if is_active is None:
            query = query.filter(FiberSpliceClosure.is_active.is_(True))
        else:
            query = query.filter(FiberSpliceClosure.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSpliceClosure.created_at, "name": FiberSpliceClosure.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, closure_id: str, payload: FiberSpliceClosureUpdate):
        closure = db.get(FiberSpliceClosure, closure_id)
        if not closure:
            raise HTTPException(status_code=404, detail="Fiber splice closure not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(closure, key, value)
        db.commit()
        db.refresh(closure)
        return closure

    @staticmethod
    def delete(db: Session, closure_id: str):
        closure = db.get(FiberSpliceClosure, closure_id)
        if not closure:
            raise HTTPException(status_code=404, detail="Fiber splice closure not found")
        closure.is_active = False
        db.commit()


class FiberSplices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSpliceCreate):
        data = payload.model_dump(exclude={"position"})
        if payload.closure_id and payload.from_strand_id and payload.to_strand_id:
            closure = db.get(FiberSpliceClosure, payload.closure_id)
            if not closure:
                raise HTTPException(status_code=404, detail="Fiber splice closure not found")
            from_strand = db.get(FiberStrand, payload.from_strand_id)
            if not from_strand:
                raise HTTPException(status_code=404, detail="Fiber strand not found")
            to_strand = db.get(FiberStrand, payload.to_strand_id)
            if not to_strand:
                raise HTTPException(status_code=404, detail="Fiber strand not found")
        elif payload.tray_id:
            tray = db.get(FiberSpliceTray, payload.tray_id)
            if not tray:
                raise HTTPException(status_code=404, detail="Fiber splice tray not found")
            data["tray_id"] = tray.id
            data["closure_id"] = tray.closure_id
            if not payload.from_strand_id or not payload.to_strand_id:
                base_number = payload.position or 1
                cable_name = f"tray-{tray.id}"
                from_strand = FiberStrand(
                    cable_name=cable_name,
                    strand_number=base_number * 2 - 1,
                )
                to_strand = FiberStrand(
                    cable_name=cable_name,
                    strand_number=base_number * 2,
                )
                db.add(from_strand)
                db.add(to_strand)
                db.flush()
                data["from_strand_id"] = from_strand.id
                data["to_strand_id"] = to_strand.id
        splice = FiberSplice(**data)
        db.add(splice)
        db.commit()
        db.refresh(splice)
        splice.position = payload.position
        return splice

    @staticmethod
    def get(db: Session, splice_id: str):
        splice = db.get(FiberSplice, splice_id)
        if not splice:
            raise HTTPException(status_code=404, detail="Fiber splice not found")
        return splice

    @staticmethod
    def list(
        db: Session,
        closure_id: str | None = None,
        strand_id: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
        tray_id: str | None = None,
    ):
        query = db.query(FiberSplice)
        if closure_id:
            query = query.filter(FiberSplice.closure_id == closure_id)
        if tray_id:
            query = query.filter(FiberSplice.tray_id == tray_id)
        if strand_id:
            query = query.filter(
                (FiberSplice.from_strand_id == strand_id)
                | (FiberSplice.to_strand_id == strand_id)
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSplice.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, splice_id: str, payload: FiberSpliceUpdate):
        splice = db.get(FiberSplice, splice_id)
        if not splice:
            raise HTTPException(status_code=404, detail="Fiber splice not found")
        data = payload.model_dump(exclude_unset=True)
        if "closure_id" in data:
            closure = db.get(FiberSpliceClosure, data["closure_id"])
            if not closure:
                raise HTTPException(status_code=404, detail="Fiber splice closure not found")
        if "from_strand_id" in data:
            from_strand = db.get(FiberStrand, data["from_strand_id"])
            if not from_strand:
                raise HTTPException(status_code=404, detail="Fiber strand not found")
        if "to_strand_id" in data:
            to_strand = db.get(FiberStrand, data["to_strand_id"])
            if not to_strand:
                raise HTTPException(status_code=404, detail="Fiber strand not found")
        for key, value in data.items():
            setattr(splice, key, value)
        db.commit()
        db.refresh(splice)
        return splice

    @staticmethod
    def delete(db: Session, splice_id: str):
        splice = db.get(FiberSplice, splice_id)
        if not splice:
            raise HTTPException(status_code=404, detail="Fiber splice not found")
        db.delete(splice)
        db.commit()

    @staticmethod
    def trace_path(db: Session, strand_id: str, max_hops: int = 25):
        strand = db.get(FiberStrand, strand_id)
        if not strand:
            raise HTTPException(status_code=404, detail="Fiber strand not found")
        def _serialize(obj, fields):
            data = {}
            for field in fields:
                value = getattr(obj, field, None)
                if hasattr(value, "value"):
                    value = value.value
                data[field] = value
            return data

        def _resolve_endpoint(endpoint_type, endpoint_id):
            if not endpoint_type or not endpoint_id:
                return {"endpoint_type": None, "endpoint_id": None, "label": None, "data": None}
            mapping = {
                FiberEndpointType.olt_port: (
                    OltCardPort,
                    "port_number",
                    ["id", "card_id", "port_number", "name", "port_type", "is_active"],
                ),
                FiberEndpointType.splitter_port: (
                    SplitterPort,
                    "port_number",
                    ["id", "splitter_id", "port_number", "port_type", "is_active"],
                ),
                FiberEndpointType.fdh: (
                    FdhCabinet,
                    "name",
                    ["id", "name", "code", "region_id", "is_active"],
                ),
                FiberEndpointType.ont: (
                    OntUnit,
                    "serial_number",
                    ["id", "serial_number", "model", "vendor", "firmware_version", "is_active"],
                ),
                FiberEndpointType.splice_closure: (
                    FiberSpliceClosure,
                    "name",
                    ["id", "name", "is_active"],
                ),
            }
            model_info = mapping.get(endpoint_type)
            if not model_info:
                return {
                    "endpoint_type": endpoint_type.value if hasattr(endpoint_type, "value") else str(endpoint_type),
                    "endpoint_id": str(endpoint_id),
                    "label": None,
                    "data": None,
                }
            model, label_field, fields = model_info
            record = db.get(model, endpoint_id)
            label = getattr(record, label_field, None) if record else None
            return {
                "endpoint_type": endpoint_type.value if hasattr(endpoint_type, "value") else str(endpoint_type),
                "endpoint_id": str(endpoint_id),
                "label": label,
                "data": _serialize(record, fields) if record else None,
            }

        path = [
            {
                "segment_type": "strand",
                "strand_id": str(strand.id),
                "upstream": _resolve_endpoint(strand.upstream_type, strand.upstream_id),
                "downstream": _resolve_endpoint(strand.downstream_type, strand.downstream_id),
            }
        ]
        visited = {strand.id}
        current = strand
        hops = 0
        while hops < max_hops:
            splice = (
                db.query(FiberSplice)
                .filter(FiberSplice.from_strand_id == current.id)
                .first()
            )
            if not splice:
                break
            next_strand = db.get(FiberStrand, splice.to_strand_id)
            if not next_strand or next_strand.id in visited:
                break
            path.append(
                {
                    "segment_type": "splice",
                    "splice_id": str(splice.id),
                    "closure_id": str(splice.closure_id),
                    "from_strand_id": str(splice.from_strand_id),
                    "to_strand_id": str(splice.to_strand_id),
                }
            )
            path.append(
                {
                    "segment_type": "strand",
                    "strand_id": str(next_strand.id),
                    "upstream": _resolve_endpoint(next_strand.upstream_type, next_strand.upstream_id),
                    "downstream": _resolve_endpoint(next_strand.downstream_type, next_strand.downstream_id),
                }
            )
            visited.add(next_strand.id)
            current = next_strand
            hops += 1
        return path

    @staticmethod
    def trace_response(db: Session, strand_id: str, max_hops: int = 25):
        return {"segments": FiberSplices.trace_path(db, strand_id, max_hops)}


class SplitterPortAssignments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SplitterPortAssignmentCreate):
        assignment = SplitterPortAssignment(**payload.model_dump())
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def get(db: Session, assignment_id: str):
        assignment = db.get(SplitterPortAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="Splitter port assignment not found")
        return assignment

    @staticmethod
    def list(
        db: Session,
        splitter_port_id: str | None,
        account_id: str | None,
        subscription_id: str | None,
        active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SplitterPortAssignment)
        if splitter_port_id:
            query = query.filter(SplitterPortAssignment.splitter_port_id == splitter_port_id)
        if account_id:
            query = query.filter(SplitterPortAssignment.account_id == account_id)
        if subscription_id:
            query = query.filter(SplitterPortAssignment.subscription_id == subscription_id)
        if active is None:
            query = query.filter(SplitterPortAssignment.active.is_(True))
        else:
            query = query.filter(SplitterPortAssignment.active == active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": SplitterPortAssignment.created_at,
                "assigned_at": SplitterPortAssignment.assigned_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, assignment_id: str, payload: SplitterPortAssignmentUpdate):
        assignment = db.get(SplitterPortAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="Splitter port assignment not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(assignment, key, value)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def delete(db: Session, assignment_id: str):
        assignment = db.get(SplitterPortAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="Splitter port assignment not found")
        db.delete(assignment)
        db.commit()


class FiberSpliceTrays(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSpliceTrayCreate):
        tray = FiberSpliceTray(**payload.model_dump())
        db.add(tray)
        db.commit()
        db.refresh(tray)
        return tray

    @staticmethod
    def get(db: Session, tray_id: str):
        tray = db.get(FiberSpliceTray, tray_id)
        if not tray:
            raise HTTPException(status_code=404, detail="Fiber splice tray not found")
        return tray

    @staticmethod
    def list(
        db: Session,
        closure_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberSpliceTray)
        if closure_id:
            query = query.filter(FiberSpliceTray.closure_id == closure_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSpliceTray.created_at, "tray_number": FiberSpliceTray.tray_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, tray_id: str, payload: FiberSpliceTrayUpdate):
        tray = db.get(FiberSpliceTray, tray_id)
        if not tray:
            raise HTTPException(status_code=404, detail="Fiber splice tray not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(tray, key, value)
        db.commit()
        db.refresh(tray)
        return tray

    @staticmethod
    def delete(db: Session, tray_id: str):
        tray = db.get(FiberSpliceTray, tray_id)
        if not tray:
            raise HTTPException(status_code=404, detail="Fiber splice tray not found")
        db.delete(tray)
        db.commit()


class FiberTerminationPoints(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberTerminationPointCreate):
        point = FiberTerminationPoint(**payload.model_dump())
        db.add(point)
        db.commit()
        db.refresh(point)
        return point

    @staticmethod
    def get(db: Session, point_id: str):
        point = db.get(FiberTerminationPoint, point_id)
        if not point:
            raise HTTPException(status_code=404, detail="Fiber termination point not found")
        return point

    @staticmethod
    def list(
        db: Session,
        endpoint_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberTerminationPoint)
        if endpoint_type:
            query = query.filter(
                FiberTerminationPoint.endpoint_type
                == validate_enum(endpoint_type, ODNEndpointType, "endpoint_type")
            )
        if is_active is None:
            query = query.filter(FiberTerminationPoint.is_active.is_(True))
        else:
            query = query.filter(FiberTerminationPoint.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberTerminationPoint.created_at, "name": FiberTerminationPoint.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, point_id: str, payload: FiberTerminationPointUpdate):
        point = db.get(FiberTerminationPoint, point_id)
        if not point:
            raise HTTPException(status_code=404, detail="Fiber termination point not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(point, key, value)
        db.commit()
        db.refresh(point)
        return point

    @staticmethod
    def delete(db: Session, point_id: str):
        point = db.get(FiberTerminationPoint, point_id)
        if not point:
            raise HTTPException(status_code=404, detail="Fiber termination point not found")
        db.delete(point)
        db.commit()


class FiberSegments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSegmentCreate):
        segment = FiberSegment(**payload.model_dump())
        db.add(segment)
        db.commit()
        db.refresh(segment)
        return segment

    @staticmethod
    def get(db: Session, segment_id: str):
        segment = db.get(FiberSegment, segment_id)
        if not segment:
            raise HTTPException(status_code=404, detail="Fiber segment not found")
        return segment

    @staticmethod
    def list(
        db: Session,
        segment_type: str | None,
        fiber_strand_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberSegment)
        if segment_type:
            query = query.filter(
                FiberSegment.segment_type
                == validate_enum(segment_type, FiberSegmentType, "segment_type")
            )
        if fiber_strand_id:
            query = query.filter(FiberSegment.fiber_strand_id == fiber_strand_id)
        if is_active is None:
            query = query.filter(FiberSegment.is_active.is_(True))
        else:
            query = query.filter(FiberSegment.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSegment.created_at, "name": FiberSegment.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, segment_id: str, payload: FiberSegmentUpdate):
        segment = db.get(FiberSegment, segment_id)
        if not segment:
            raise HTTPException(status_code=404, detail="Fiber segment not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(segment, key, value)
        db.commit()
        db.refresh(segment)
        return segment

    @staticmethod
    def delete(db: Session, segment_id: str):
        segment = db.get(FiberSegment, segment_id)
        if not segment:
            raise HTTPException(status_code=404, detail="Fiber segment not found")
        segment.is_active = False
        db.commit()


class PonPortSplitterLinks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PonPortSplitterLinkCreate):
        link = PonPortSplitterLink(**payload.model_dump())
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(PonPortSplitterLink, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="PON port link not found")
        return link

    @staticmethod
    def list(
        db: Session,
        pon_port_id: str | None,
        splitter_port_id: str | None,
        active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PonPortSplitterLink)
        if pon_port_id:
            query = query.filter(PonPortSplitterLink.pon_port_id == pon_port_id)
        if splitter_port_id:
            query = query.filter(PonPortSplitterLink.splitter_port_id == splitter_port_id)
        if active is None:
            query = query.filter(PonPortSplitterLink.active.is_(True))
        else:
            query = query.filter(PonPortSplitterLink.active == active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PonPortSplitterLink.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, link_id: str, payload: PonPortSplitterLinkUpdate):
        link = db.get(PonPortSplitterLink, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="PON port link not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(link, key, value)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str):
        link = db.get(PonPortSplitterLink, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="PON port link not found")
        db.delete(link)
        db.commit()


class OltPowerUnits(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OltPowerUnitCreate):
        unit = OltPowerUnit(**payload.model_dump())
        db.add(unit)
        db.commit()
        db.refresh(unit)
        return unit

    @staticmethod
    def get(db: Session, unit_id: str):
        unit = db.get(OltPowerUnit, unit_id)
        if not unit:
            raise HTTPException(status_code=404, detail="OLT power unit not found")
        return unit

    @staticmethod
    def list(
        db: Session,
        olt_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OltPowerUnit)
        if olt_id:
            query = query.filter(OltPowerUnit.olt_id == olt_id)
        if is_active is None:
            query = query.filter(OltPowerUnit.is_active.is_(True))
        else:
            query = query.filter(OltPowerUnit.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltPowerUnit.created_at, "slot": OltPowerUnit.slot},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, unit_id: str, payload: OltPowerUnitUpdate):
        unit = db.get(OltPowerUnit, unit_id)
        if not unit:
            raise HTTPException(status_code=404, detail="OLT power unit not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(unit, key, value)
        db.commit()
        db.refresh(unit)
        return unit

    @staticmethod
    def delete(db: Session, unit_id: str):
        unit = db.get(OltPowerUnit, unit_id)
        if not unit:
            raise HTTPException(status_code=404, detail="OLT power unit not found")
        unit.is_active = False
        db.commit()


class OltSfpModules(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OltSfpModuleCreate):
        module = OltSfpModule(**payload.model_dump())
        db.add(module)
        db.commit()
        db.refresh(module)
        return module

    @staticmethod
    def get(db: Session, module_id: str):
        module = db.get(OltSfpModule, module_id)
        if not module:
            raise HTTPException(status_code=404, detail="OLT SFP module not found")
        return module

    @staticmethod
    def list(
        db: Session,
        olt_card_port_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OltSfpModule)
        if olt_card_port_id:
            query = query.filter(OltSfpModule.olt_card_port_id == olt_card_port_id)
        if is_active is None:
            query = query.filter(OltSfpModule.is_active.is_(True))
        else:
            query = query.filter(OltSfpModule.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltSfpModule.created_at, "serial_number": OltSfpModule.serial_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, module_id: str, payload: OltSfpModuleUpdate):
        module = db.get(OltSfpModule, module_id)
        if not module:
            raise HTTPException(status_code=404, detail="OLT SFP module not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(module, key, value)
        db.commit()
        db.refresh(module)
        return module

    @staticmethod
    def delete(db: Session, module_id: str):
        module = db.get(OltSfpModule, module_id)
        if not module:
            raise HTTPException(status_code=404, detail="OLT SFP module not found")
        module.is_active = False
        db.commit()


cpe_devices = CPEDevices()
ports = Ports()
vlans = Vlans()
port_vlans = PortVlans()
ip_assignments = IPAssignments()
ip_pools = IpPools()
ip_blocks = IpBlocks()
ipv4_addresses = IPv4Addresses()
ipv6_addresses = IPv6Addresses()
olt_devices = OLTDevices()
pon_ports = PonPorts()
ont_units = OntUnits()
ont_assignments = OntAssignments()
olt_shelves = OltShelves()
olt_cards = OltCards()
olt_card_ports = OltCardPorts()
fdh_cabinets = FdhCabinets()
splitters = Splitters()
splitter_ports = SplitterPorts()
splitter_port_assignments = SplitterPortAssignments()
fiber_strands = FiberStrands()
fiber_splice_closures = FiberSpliceClosures()
fiber_splice_trays = FiberSpliceTrays()
fiber_splices = FiberSplices()
fiber_termination_points = FiberTerminationPoints()
fiber_segments = FiberSegments()
pon_port_splitter_links = PonPortSplitterLinks()
olt_power_units = OltPowerUnits()
olt_sfp_modules = OltSfpModules()
