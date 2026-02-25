"""OLT infrastructure services."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    OltCard,
    OltCardPort,
    OLTDevice,
    OltPortType,
    OltPowerUnit,
    OltSfpModule,
    OltShelf,
    OntAssignment,
    OntUnit,
    PonPort,
)
from app.schemas.network import (
    OltCardCreate,
    OltCardPortCreate,
    OltCardPortUpdate,
    OltCardUpdate,
    OLTDeviceUpdate,
    OltPowerUnitUpdate,
    OltSfpModuleUpdate,
    OltShelfCreate,
    OltShelfUpdate,
    OntAssignmentUpdate,
    OntUnitUpdate,
    PonPortCreate,
    PonPortUpdate,
)
from app.services.common import coerce_uuid
from app.services.crud import CRUDManager
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.query_builders import apply_active_state, apply_optional_equals

logger = logging.getLogger(__name__)


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
    ) -> list[OLTDevice]:
        stmt = select(OLTDevice)
        stmt = apply_active_state(stmt, OLTDevice.is_active, is_active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OLTDevice.created_at, "name": OLTDevice.name},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @classmethod
    def get(cls, db: Session, device_id: str) -> OLTDevice:
        return super().get(db, device_id)

    @classmethod
    def update(cls, db: Session, device_id: str, payload: OLTDeviceUpdate) -> OLTDevice:
        return super().update(db, device_id, payload)

    @classmethod
    def delete(cls, db: Session, device_id: str) -> None:
        return super().delete(db, device_id)


class PonPorts(CRUDManager[PonPort]):
    model = PonPort
    not_found_detail = "PON port not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: PonPortCreate) -> PonPort:
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
            db.flush()
        data = payload.model_dump()
        if payload.card_id and not payload.olt_card_port_id and card_port:
            data["olt_card_port_id"] = card_port.id
        port = PonPort(**data)
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @classmethod
    def get(cls, db: Session, port_id: str) -> PonPort:
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
    ) -> list[PonPort]:
        stmt = select(PonPort)
        if card_id:
            stmt = stmt.join(OltCardPort, PonPort.olt_card_port_id == OltCardPort.id)
            stmt = stmt.filter(OltCardPort.card_id == coerce_uuid(card_id))
        stmt = apply_optional_equals(stmt, {PonPort.olt_id: olt_id})
        stmt = apply_active_state(stmt, PonPort.is_active, is_active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": PonPort.created_at, "name": PonPort.name},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, port_id: str, payload: PonPortUpdate) -> PonPort:
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
    def delete(cls, db: Session, port_id: str) -> None:
        return super().delete(db, port_id)

    @staticmethod
    def utilization(db: Session, olt_id: str | None) -> dict[str, object]:
        total_stmt = select(func.count(PonPort.id)).where(PonPort.is_active.is_(True))
        if olt_id:
            total_stmt = total_stmt.where(PonPort.olt_id == olt_id)
        total_ports = db.scalar(total_stmt) or 0

        assigned_stmt = (
            select(func.count(func.distinct(OntAssignment.pon_port_id)))
            .where(OntAssignment.active.is_(True))
        )
        if olt_id:
            assigned_stmt = assigned_stmt.where(
                OntAssignment.pon_port_id.in_(
                    select(PonPort.id).where(PonPort.olt_id == olt_id)
                )
            )
        assigned_count = db.scalar(assigned_stmt) or 0

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
    ) -> list[OntUnit]:
        stmt = select(OntUnit)
        stmt = apply_active_state(stmt, OntUnit.is_active, is_active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OntUnit.created_at, "serial_number": OntUnit.serial_number},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def list_advanced(
        db: Session,
        *,
        olt_id: str | None = None,
        pon_port_id: str | None = None,
        zone_id: str | None = None,
        signal_quality: str | None = None,
        online_status: str | None = None,
        vendor: str | None = None,
        search: str | None = None,
        is_active: bool | None = None,
        order_by: str = "serial_number",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[OntUnit], int]:
        """Advanced ONT query with multi-dimensional filtering.

        Returns:
            Tuple of (filtered ONTs, total count before pagination).
        """
        from app.services.network.olt_polling import get_signal_thresholds

        stmt = select(OntUnit)

        # Filter by OLT or PON port via active assignment join
        if olt_id or pon_port_id:
            stmt = stmt.join(
                OntAssignment,
                (OntAssignment.ont_unit_id == OntUnit.id) & (OntAssignment.active.is_(True)),
            )
            if pon_port_id:
                stmt = stmt.where(OntAssignment.pon_port_id == coerce_uuid(pon_port_id))
            elif olt_id:
                stmt = stmt.join(PonPort, PonPort.id == OntAssignment.pon_port_id)
                stmt = stmt.where(PonPort.olt_id == coerce_uuid(olt_id))

        # Filter by zone
        if zone_id:
            stmt = stmt.where(OntUnit.zone_id == coerce_uuid(zone_id))

        # Filter by active state
        stmt = apply_active_state(stmt, OntUnit.is_active, is_active)

        # Filter by online status
        from app.models.network import OnuOnlineStatus

        if online_status and online_status in ("online", "offline", "unknown"):
            stmt = stmt.where(OntUnit.online_status == OnuOnlineStatus(online_status))

        # Filter by vendor
        if vendor:
            stmt = stmt.where(OntUnit.vendor.ilike(f"%{vendor}%"))

        # Text search on serial number
        if search:
            stmt = stmt.where(OntUnit.serial_number.ilike(f"%{search}%"))

        # Filter by signal quality using thresholds
        if signal_quality and signal_quality in ("good", "warning", "critical"):
            warn, crit = get_signal_thresholds(db)
            if signal_quality == "critical":
                stmt = stmt.where(OntUnit.olt_rx_signal_dbm < crit)
            elif signal_quality == "warning":
                stmt = stmt.where(
                    OntUnit.olt_rx_signal_dbm.between(crit, warn)
                )
            elif signal_quality == "good":
                stmt = stmt.where(OntUnit.olt_rx_signal_dbm >= warn)

        # Count before pagination
        count_stmt = stmt.with_only_columns(func.count()).order_by(None)
        total = db.scalar(count_stmt) or 0

        # Ordering — include signal-based sorting
        allowed = {
            "serial_number": OntUnit.serial_number,
            "created_at": OntUnit.created_at,
            "signal": OntUnit.olt_rx_signal_dbm,
            "last_seen": OntUnit.last_seen_at,
            "vendor": OntUnit.vendor,
        }
        stmt = _apply_ordering(stmt, order_by, order_dir, allowed)
        results = list(db.scalars(_apply_pagination(stmt, limit, offset)).all())
        return results, total

    @classmethod
    def get(cls, db: Session, unit_id: str) -> OntUnit:
        return super().get(db, unit_id)

    @classmethod
    def update(cls, db: Session, unit_id: str, payload: OntUnitUpdate) -> OntUnit:
        return super().update(db, unit_id, payload)

    @classmethod
    def delete(cls, db: Session, unit_id: str) -> None:
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
    ) -> list[OntAssignment]:
        stmt = select(OntAssignment)
        stmt = apply_optional_equals(
            stmt,
            {
                OntAssignment.ont_unit_id: ont_unit_id,
                OntAssignment.pon_port_id: pon_port_id,
            },
        )
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OntAssignment.created_at, "active": OntAssignment.active},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @classmethod
    def get(cls, db: Session, assignment_id: str) -> OntAssignment:
        return super().get(db, assignment_id)

    @classmethod
    def update(cls, db: Session, assignment_id: str, payload: OntAssignmentUpdate) -> OntAssignment:
        return super().update(db, assignment_id, payload)

    @classmethod
    def delete(cls, db: Session, assignment_id: str) -> None:
        return super().delete(db, assignment_id)


class OltShelves(CRUDManager[OltShelf]):
    model = OltShelf
    not_found_detail = "OLT shelf not found"

    @staticmethod
    def create(db: Session, payload: OltShelfCreate) -> OltShelf:
        olt = db.get(OLTDevice, payload.olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")
        shelf = OltShelf(**payload.model_dump())
        db.add(shelf)
        db.commit()
        db.refresh(shelf)
        return shelf

    @classmethod
    def get(cls, db: Session, shelf_id: str) -> OltShelf:
        return super().get(db, shelf_id)

    @staticmethod
    def list(
        db: Session,
        olt_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[OltShelf]:
        stmt = select(OltShelf)
        stmt = apply_optional_equals(stmt, {OltShelf.olt_id: olt_id})
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OltShelf.created_at, "shelf_number": OltShelf.shelf_number},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, shelf_id: str, payload: OltShelfUpdate) -> OltShelf:
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
    def delete(cls, db: Session, shelf_id: str) -> None:
        return super().delete(db, shelf_id)


class OltCards(CRUDManager[OltCard]):
    model = OltCard
    not_found_detail = "OLT card not found"

    @staticmethod
    def create(db: Session, payload: OltCardCreate) -> OltCard:
        shelf = db.get(OltShelf, payload.shelf_id)
        if not shelf:
            raise HTTPException(status_code=404, detail="OLT shelf not found")
        card = OltCard(**payload.model_dump())
        db.add(card)
        db.commit()
        db.refresh(card)
        return card

    @classmethod
    def get(cls, db: Session, card_id: str) -> OltCard:
        return super().get(db, card_id)

    @staticmethod
    def list(
        db: Session,
        shelf_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[OltCard]:
        stmt = select(OltCard)
        stmt = apply_optional_equals(stmt, {OltCard.shelf_id: shelf_id})
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OltCard.created_at, "slot_number": OltCard.slot_number},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, card_id: str, payload: OltCardUpdate) -> OltCard:
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
    def delete(cls, db: Session, card_id: str) -> None:
        return super().delete(db, card_id)


class OltCardPorts(CRUDManager[OltCardPort]):
    model = OltCardPort
    not_found_detail = "OLT card port not found"

    @staticmethod
    def create(db: Session, payload: OltCardPortCreate) -> OltCardPort:
        card = db.get(OltCard, payload.card_id)
        if not card:
            raise HTTPException(status_code=404, detail="OLT card not found")
        port = OltCardPort(**payload.model_dump())
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @classmethod
    def get(cls, db: Session, port_id: str) -> OltCardPort:
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
    ) -> list[OltCardPort]:
        stmt = select(OltCardPort)
        stmt = apply_optional_equals(stmt, {OltCardPort.card_id: card_id})
        if port_type:
            stmt = stmt.filter(
                OltCardPort.port_type == _validate_enum(port_type, OltPortType, "port_type")
            )
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OltCardPort.created_at, "port_number": OltCardPort.port_number},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, port_id: str, payload: OltCardPortUpdate) -> OltCardPort:
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
    def delete(cls, db: Session, port_id: str) -> None:
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
    ) -> list[OltPowerUnit]:
        stmt = select(OltPowerUnit)
        stmt = apply_optional_equals(stmt, {OltPowerUnit.olt_id: olt_id})
        stmt = apply_active_state(stmt, OltPowerUnit.is_active, is_active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OltPowerUnit.created_at, "slot": OltPowerUnit.slot},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @classmethod
    def get(cls, db: Session, unit_id: str) -> OltPowerUnit:
        return super().get(db, unit_id)

    @classmethod
    def update(cls, db: Session, unit_id: str, payload: OltPowerUnitUpdate) -> OltPowerUnit:
        return super().update(db, unit_id, payload)

    @classmethod
    def delete(cls, db: Session, unit_id: str) -> None:
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
    ) -> list[OltSfpModule]:
        stmt = select(OltSfpModule)
        stmt = apply_optional_equals(
            stmt,
            {OltSfpModule.olt_card_port_id: olt_card_port_id},
        )
        stmt = apply_active_state(stmt, OltSfpModule.is_active, is_active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OltSfpModule.created_at, "serial_number": OltSfpModule.serial_number},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @classmethod
    def get(cls, db: Session, module_id: str) -> OltSfpModule:
        return super().get(db, module_id)

    @classmethod
    def update(cls, db: Session, module_id: str, payload: OltSfpModuleUpdate) -> OltSfpModule:
        return super().update(db, module_id, payload)

    @classmethod
    def delete(cls, db: Session, module_id: str) -> None:
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
