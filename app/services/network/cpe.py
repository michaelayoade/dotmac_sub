"""CPE device and port services."""

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    DeviceType,
    Port,
    PortStatus,
    PortType,
    PortVlan,
    Vlan,
)
from app.schemas.network import (
    CPEDeviceCreate,
    CPEDeviceUpdate,
    PortCreate,
    PortUpdate,
    PortVlanCreate,
    PortVlanUpdate,
    VlanCreate,
    VlanUpdate,
)
from app.services import settings_spec
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.response import ListResponseMixin
from app.validators import network as network_validators

logger = logging.getLogger(__name__)


def _auto_register_tr069_device(db: Session, device: CPEDevice) -> None:
    if not device.serial_number:
        return
    from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice

    existing = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.cpe_device_id == device.id)
        .first()
    )
    if existing:
        return
    acs_server_id = settings_spec.resolve_value(
        db, SettingDomain.tr069, "default_acs_server_id"
    )
    server = None
    if acs_server_id:
        server = db.get(Tr069AcsServer, acs_server_id)
        if server and not server.is_active:
            server = None
    if not server:
        server = (
            db.query(Tr069AcsServer)
            .filter(Tr069AcsServer.is_active.is_(True))
            .order_by(Tr069AcsServer.created_at.asc())
            .first()
        )
    if not server:
        logger.info(
            "Skipping TR-069 auto-registration for CPE %s: no active ACS server.",
            device.id,
        )
        return
    existing = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.acs_server_id == server.id)
        .filter(Tr069CpeDevice.serial_number == device.serial_number)
        .first()
    )
    if existing:
        if existing.cpe_device_id and existing.cpe_device_id != device.id:
            logger.warning(
                "TR-069 device %s already linked to another CPE device.",
                existing.id,
            )
            return
        existing.cpe_device_id = device.id
        existing.is_active = True
    else:
        db.add(
            Tr069CpeDevice(
                acs_server_id=server.id,
                cpe_device_id=device.id,
                serial_number=device.serial_number,
                is_active=True,
            )
        )
    db.commit()


class CPEDevices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CPEDeviceCreate):
        network_validators.validate_cpe_device_links(
            db,
            str(payload.subscriber_id),
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
                data["device_type"] = _validate_enum(
                    default_type, DeviceType, "device_type"
                )
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.network, "default_device_status"
            )
            if default_status:
                data["status"] = _validate_enum(
                    default_status, DeviceStatus, "status"
                )
        device = CPEDevice(**data)
        db.add(device)
        db.commit()
        db.refresh(device)
        _auto_register_tr069_device(db, device)
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
        subscriber_id: str | None,
        subscription_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CPEDevice)
        if subscriber_id:
            query = query.filter(CPEDevice.subscriber_id == subscriber_id)
        if subscription_id:
            query = query.filter(CPEDevice.subscription_id == subscription_id)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CPEDevice.created_at, "updated_at": CPEDevice.updated_at},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, device_id: str, payload: CPEDeviceUpdate):
        device = db.get(CPEDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="CPE device not found")
        data = payload.model_dump(exclude_unset=True)
        subscriber_id = str(data.get("subscriber_id", device.subscriber_id))
        subscription_id = data.get("subscription_id", device.subscription_id)
        service_address_id = data.get("service_address_id", device.service_address_id)
        network_validators.validate_cpe_device_links(
            db,
            subscriber_id,
            str(subscription_id) if subscription_id else None,
            str(service_address_id) if service_address_id else None,
        )
        for key, value in data.items():
            setattr(device, key, value)
        db.commit()
        db.refresh(device)
        _auto_register_tr069_device(db, device)
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
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "port_type" not in fields_set:
            default_type = settings_spec.resolve_value(
                db, SettingDomain.network, "default_port_type"
            )
            if default_type:
                data["port_type"] = _validate_enum(
                    default_type, PortType, "port_type"
                )
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.network, "default_port_status"
            )
            if default_status:
                data["status"] = _validate_enum(
                    default_status, PortStatus, "status"
                )
        port = Port(**data)
        db.add(port)
        db.commit()
        db.refresh(port)
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
        device_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Port)
        if device_id:
            query = query.filter(Port.device_id == device_id)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Port.created_at, "name": Port.name},
        )
        return _apply_pagination(query, limit, offset).all()

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
        db.delete(port)
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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Vlan.created_at, "tag": Vlan.tag},
        )
        return _apply_pagination(query, limit, offset).all()

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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"port_id": PortVlan.port_id, "vlan_id": PortVlan.vlan_id},
        )
        return _apply_pagination(query, limit, offset).all()

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


cpe_devices = CPEDevices()
ports = Ports()
vlans = Vlans()
port_vlans = PortVlans()
