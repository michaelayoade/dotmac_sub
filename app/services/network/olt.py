"""OLT infrastructure services."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import (
    OLTDevice,
    OltCard,
    OltCardPort,
    OltPortType,
    OltPowerUnit,
    OltSfpModule,
    OltShelf,
    OntAssignment,
    OntUnit,
    PonPort,
)
from app.schemas.network import (
    OLTDeviceUpdate,
    OltCardCreate,
    OltCardPortCreate,
    OltCardPortUpdate,
    OltCardUpdate,
    OltPowerUnitUpdate,
    OltSfpModuleUpdate,
    OltShelfCreate,
    OltShelfUpdate,
    OntAssignmentCreate,
    OntAssignmentUpdate,
    OntUnitUpdate,
    PonPortCreate,
    PonPortUpdate,
)
from app.services import settings_spec
from app.services.crud import CRUDManager
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.common import coerce_uuid
from app.services.query_builders import apply_active_state, apply_optional_equals
from app.services.response import ListResponseMixin


class OLTDevices(CRUDManager[OLTDevice]):
    model = OLTDevice
    not_found_detail = "OLT device not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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
        query = apply_active_state(query, OLTDevice.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OLTDevice.created_at, "name": OLTDevice.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, device_id: str):
        return super().get(db, device_id)

    @classmethod
    def update(cls, db: Session, device_id: str, payload: OLTDeviceUpdate):
        return super().update(db, device_id, payload)

    @classmethod
    def delete(cls, db: Session, device_id: str):
        return super().delete(db, device_id)


class PonPorts(CRUDManager[PonPort]):
    model = PonPort
    not_found_detail = "PON port not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: PonPortCreate):
        olt = db.get(OLTDevice, payload.olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")
        card_port: OltCardPort | None = None
        if payload.olt_card_port_id:
            card_port = db.get(OltCardPort, payload.olt_card_port_id)
            if not card_port:
                raise HTTPException(status_code=404, detail="OLT card port not found")
        elif payload.card_id:
            if payload.port_number is None:
                raise HTTPException(status_code=400, detail="port_number is required when card_id is provided")
            card = db.get(OltCard, payload.card_id)
            if not card:
                raise HTTPException(status_code=404, detail="OLT card not found")
            card_port = OltCardPort(
                card_id=payload.card_id,
                port_number=payload.port_number,
                port_type=OltPortType.pon,
                name=payload.name,
                is_active=True,
            )
            db.add(card_port)
            db.commit()
            db.refresh(card_port)
        data = payload.model_dump()
        if payload.card_id and not payload.olt_card_port_id and card_port:
            data["olt_card_port_id"] = card_port.id
        port = PonPort(**data)
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @classmethod
    def get(cls, db: Session, port_id: str):
        return super().get(db, port_id)

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        card_id: str | None = None,
        olt_id: str | None = None,
        is_active: bool | None = None,
    ):
        query = db.query(PonPort)
        if card_id:
            query = query.join(OltCardPort, PonPort.olt_card_port_id == OltCardPort.id)
            query = query.filter(OltCardPort.card_id == coerce_uuid(card_id))
        query = apply_optional_equals(query, {PonPort.olt_id: olt_id})
        query = apply_active_state(query, PonPort.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PonPort.created_at, "name": PonPort.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, port_id: str, payload: PonPortUpdate):
        port = PonPorts.get(db, port_id)
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

    @classmethod
    def delete(cls, db: Session, port_id: str):
        return super().delete(db, port_id)

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


class OntUnits(CRUDManager[OntUnit]):
    model = OntUnit
    not_found_detail = "ONT unit not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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
        query = apply_active_state(query, OntUnit.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OntUnit.created_at, "serial_number": OntUnit.serial_number},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, unit_id: str):
        return super().get(db, unit_id)

    @classmethod
    def update(cls, db: Session, unit_id: str, payload: OntUnitUpdate):
        return super().update(db, unit_id, payload)

    @classmethod
    def delete(cls, db: Session, unit_id: str):
        return super().delete(db, unit_id)


class OntAssignments(CRUDManager[OntAssignment]):
    model = OntAssignment
    not_found_detail = "ONT assignment not found"

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
        query = apply_optional_equals(
            query,
            {
                OntAssignment.ont_unit_id: ont_unit_id,
                OntAssignment.pon_port_id: pon_port_id,
            },
        )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OntAssignment.created_at, "active": OntAssignment.active},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, assignment_id: str):
        return super().get(db, assignment_id)

    @classmethod
    def update(cls, db: Session, assignment_id: str, payload: OntAssignmentUpdate):
        return super().update(db, assignment_id, payload)

    @classmethod
    def delete(cls, db: Session, assignment_id: str):
        return super().delete(db, assignment_id)


class OltShelves(CRUDManager[OltShelf]):
    model = OltShelf
    not_found_detail = "OLT shelf not found"

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

    @classmethod
    def get(cls, db: Session, shelf_id: str):
        return super().get(db, shelf_id)

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
        query = apply_optional_equals(query, {OltShelf.olt_id: olt_id})
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltShelf.created_at, "shelf_number": OltShelf.shelf_number},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, shelf_id: str, payload: OltShelfUpdate):
        shelf = OltShelves.get(db, shelf_id)
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

    @classmethod
    def delete(cls, db: Session, shelf_id: str):
        return super().delete(db, shelf_id)


class OltCards(CRUDManager[OltCard]):
    model = OltCard
    not_found_detail = "OLT card not found"

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

    @classmethod
    def get(cls, db: Session, card_id: str):
        return super().get(db, card_id)

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
        query = apply_optional_equals(query, {OltCard.shelf_id: shelf_id})
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltCard.created_at, "slot_number": OltCard.slot_number},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, card_id: str, payload: OltCardUpdate):
        card = OltCards.get(db, card_id)
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

    @classmethod
    def delete(cls, db: Session, card_id: str):
        return super().delete(db, card_id)


class OltCardPorts(CRUDManager[OltCardPort]):
    model = OltCardPort
    not_found_detail = "OLT card port not found"

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

    @classmethod
    def get(cls, db: Session, port_id: str):
        return super().get(db, port_id)

    @staticmethod
    def list(
        db: Session,
        card_id: str | None,
        port_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OltCardPort)
        query = apply_optional_equals(query, {OltCardPort.card_id: card_id})
        if port_type:
            query = query.filter(
                OltCardPort.port_type == _validate_enum(port_type, OltPortType, "port_type")
            )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltCardPort.created_at, "port_number": OltCardPort.port_number},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, port_id: str, payload: OltCardPortUpdate):
        port = OltCardPorts.get(db, port_id)
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

    @classmethod
    def delete(cls, db: Session, port_id: str):
        return super().delete(db, port_id)


class OltPowerUnits(CRUDManager[OltPowerUnit]):
    model = OltPowerUnit
    not_found_detail = "OLT power unit not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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
        query = apply_optional_equals(query, {OltPowerUnit.olt_id: olt_id})
        query = apply_active_state(query, OltPowerUnit.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltPowerUnit.created_at, "slot": OltPowerUnit.slot},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, unit_id: str):
        return super().get(db, unit_id)

    @classmethod
    def update(cls, db: Session, unit_id: str, payload: OltPowerUnitUpdate):
        return super().update(db, unit_id, payload)

    @classmethod
    def delete(cls, db: Session, unit_id: str):
        return super().delete(db, unit_id)


class OltSfpModules(CRUDManager[OltSfpModule]):
    model = OltSfpModule
    not_found_detail = "OLT SFP module not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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
        query = apply_optional_equals(
            query,
            {OltSfpModule.olt_card_port_id: olt_card_port_id},
        )
        query = apply_active_state(query, OltSfpModule.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltSfpModule.created_at, "serial_number": OltSfpModule.serial_number},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, module_id: str):
        return super().get(db, module_id)

    @classmethod
    def update(cls, db: Session, module_id: str, payload: OltSfpModuleUpdate):
        return super().update(db, module_id, payload)

    @classmethod
    def delete(cls, db: Session, module_id: str):
        return super().delete(db, module_id)


olt_devices = OLTDevices()
pon_ports = PonPorts()
ont_units = OntUnits()
ont_assignments = OntAssignments()
olt_shelves = OltShelves()
olt_cards = OltCards()
olt_card_ports = OltCardPorts()
olt_power_units = OltPowerUnits()
olt_sfp_modules = OltSfpModules()
