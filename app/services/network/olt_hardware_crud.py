"""CRUD managers for OLT physical hardware inventory."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OltCard,
    OltCardPort,
    OLTDevice,
    OltPortType,
    OltPowerUnit,
    OltSfpModule,
    OltShelf,
)
from app.schemas.network import (
    OltCardCreate,
    OltCardPortCreate,
    OltCardPortUpdate,
    OltCardUpdate,
    OltPowerUnitUpdate,
    OltSfpModuleUpdate,
    OltShelfCreate,
    OltShelfUpdate,
)
from app.services.crud import CRUDManager
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.query_builders import apply_active_state, apply_optional_equals


class OltShelves(CRUDManager[OltShelf]):
    model = OltShelf
    not_found_detail = "OLT shelf not found"

    @staticmethod
    def create(db: Session, payload: OltShelfCreate) -> OltShelf:  # type: ignore[override]
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
    def update(db: Session, shelf_id: str, payload: OltShelfUpdate) -> OltShelf:  # type: ignore[override]
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
    def delete(cls, db: Session, shelf_id: str) -> None:  # type: ignore[override]
        return super().delete(db, shelf_id)


class OltCards(CRUDManager[OltCard]):
    model = OltCard
    not_found_detail = "OLT card not found"

    @staticmethod
    def create(db: Session, payload: OltCardCreate) -> OltCard:  # type: ignore[override]
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
    def update(db: Session, card_id: str, payload: OltCardUpdate) -> OltCard:  # type: ignore[override]
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
    def delete(cls, db: Session, card_id: str) -> None:  # type: ignore[override]
        return super().delete(db, card_id)


class OltCardPorts(CRUDManager[OltCardPort]):
    model = OltCardPort
    not_found_detail = "OLT card port not found"

    @staticmethod
    def create(db: Session, payload: OltCardPortCreate) -> OltCardPort:  # type: ignore[override]
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
                OltCardPort.port_type
                == _validate_enum(port_type, OltPortType, "port_type")
            )
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {
                "created_at": OltCardPort.created_at,
                "port_number": OltCardPort.port_number,
            },
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, port_id: str, payload: OltCardPortUpdate) -> OltCardPort:  # type: ignore[override]
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
    def delete(cls, db: Session, port_id: str) -> None:  # type: ignore[override]
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
    def update(  # type: ignore[override]
        cls, db: Session, unit_id: str, payload: OltPowerUnitUpdate
    ) -> OltPowerUnit:
        return super().update(db, unit_id, payload)

    @classmethod
    def delete(cls, db: Session, unit_id: str) -> None:  # type: ignore[override]
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
            {
                "created_at": OltSfpModule.created_at,
                "serial_number": OltSfpModule.serial_number,
            },
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @classmethod
    def get(cls, db: Session, module_id: str) -> OltSfpModule:
        return super().get(db, module_id)

    @classmethod
    def update(  # type: ignore[override]
        cls, db: Session, module_id: str, payload: OltSfpModuleUpdate
    ) -> OltSfpModule:
        return super().update(db, module_id, payload)

    @classmethod
    def delete(cls, db: Session, module_id: str) -> None:  # type: ignore[override]
        return super().delete(db, module_id)
