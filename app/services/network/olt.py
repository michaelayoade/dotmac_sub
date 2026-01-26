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
    OLTDeviceCreate,
    OLTDeviceUpdate,
    OltCardCreate,
    OltCardPortCreate,
    OltCardPortUpdate,
    OltCardUpdate,
    OltPowerUnitCreate,
    OltPowerUnitUpdate,
    OltSfpModuleCreate,
    OltSfpModuleUpdate,
    OltShelfCreate,
    OltShelfUpdate,
    OntAssignmentCreate,
    OntAssignmentUpdate,
    OntUnitCreate,
    OntUnitUpdate,
    PonPortCreate,
    PonPortUpdate,
)
from app.services import settings_spec
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.response import ListResponseMixin


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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OLTDevice.created_at, "name": OLTDevice.name},
        )
        return _apply_pagination(query, limit, offset).all()

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
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "port_type" not in fields_set:
            default_type = settings_spec.resolve_value(
                db, SettingDomain.network, "default_olt_port_type"
            )
            if default_type:
                data["port_type"] = _validate_enum(
                    default_type, OltPortType, "port_type"
                )
        port = PonPort(**data)
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def get(db: Session, port_id: str):
        port = db.get(PonPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="PON port not found")
        return port

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
        query = db.query(PonPort)
        if olt_id:
            query = query.filter(PonPort.olt_id == olt_id)
        if is_active is None:
            query = query.filter(PonPort.is_active.is_(True))
        else:
            query = query.filter(PonPort.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PonPort.created_at, "name": PonPort.name},
        )
        return _apply_pagination(query, limit, offset).all()

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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OntUnit.created_at, "serial_number": OntUnit.serial_number},
        )
        return _apply_pagination(query, limit, offset).all()

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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OntAssignment.created_at, "active": OntAssignment.active},
        )
        return _apply_pagination(query, limit, offset).all()

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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltShelf.created_at, "shelf_number": OltShelf.shelf_number},
        )
        return _apply_pagination(query, limit, offset).all()

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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltCard.created_at, "slot_number": OltCard.slot_number},
        )
        return _apply_pagination(query, limit, offset).all()

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
        card_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OltCardPort)
        if card_id:
            query = query.filter(OltCardPort.card_id == card_id)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltCardPort.created_at, "port_number": OltCardPort.port_number},
        )
        return _apply_pagination(query, limit, offset).all()

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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltPowerUnit.created_at, "slot": OltPowerUnit.slot},
        )
        return _apply_pagination(query, limit, offset).all()

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
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OltSfpModule.created_at, "serial_number": OltSfpModule.serial_number},
        )
        return _apply_pagination(query, limit, offset).all()

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


olt_devices = OLTDevices()
pon_ports = PonPorts()
ont_units = OntUnits()
ont_assignments = OntAssignments()
olt_shelves = OltShelves()
olt_cards = OltCards()
olt_card_ports = OltCardPorts()
olt_power_units = OltPowerUnits()
olt_sfp_modules = OltSfpModules()
