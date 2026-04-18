"""OLT infrastructure services.

Transaction Policy:
- Service methods commit their own transactions via db.commit()
- Use db.flush() when creating entities that need IDs for related operations
- Use db.begin_nested() for operations requiring partial rollback capability
- Routes must NOT call db.commit() (per CLAUDE.md)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from time import sleep

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased, joinedload

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
from app.models.subscriber import Subscriber
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
    OntAssignmentCreate,
    OntAssignmentUpdate,
    OntUnitUpdate,
    PonPortCreate,
    PonPortUpdate,
)
from app.services.common import coerce_uuid
from app.services.crud import CRUDManager
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
    decode_huawei_hex_serial,
    encode_to_hex_serial,
)
from app.services.query_builders import apply_active_state, apply_optional_equals
from app.validators import network as network_validators

logger = logging.getLogger(__name__)

_CANONICAL_PON_NAME_RE = re.compile(r"^\d+/\d+/\d+$")


_ONT_STATUS_LOADS = (
    joinedload(OntUnit.tr069_acs_server),
    joinedload(OntUnit.olt_device).joinedload(OLTDevice.tr069_acs_server),
)


def _canonical_pon_name_from_card_port(
    db: Session,
    card_port: OltCardPort,
) -> str:
    card = db.get(OltCard, card_port.card_id)
    shelf = db.get(OltShelf, card.shelf_id) if card else None
    if shelf and card:
        return f"{shelf.shelf_number}/{card.slot_number}/{card_port.port_number}"
    if getattr(card_port, "name", None):
        return str(card_port.name)
    return f"pon-{card_port.port_number}"


def _parse_canonical_pon_name(name: str | None) -> tuple[str, int] | None:
    text = str(name or "").strip()
    if not _CANONICAL_PON_NAME_RE.fullmatch(text):
        return None
    board, port = text.rsplit("/", 1)
    return board, int(port)


def _validate_assignment_target(
    db: Session,
    *,
    ont_unit_id: object,
    pon_port_id: object | None,
    active: bool,
    current_assignment_id: object | None = None,
) -> tuple[OntUnit, PonPort | None]:
    ont = db.scalar(select(OntUnit).where(OntUnit.id == ont_unit_id).with_for_update())
    if not ont:
        raise HTTPException(status_code=404, detail="ONT unit not found")

    pon_port: PonPort | None = None
    if pon_port_id is not None:
        pon_port = db.scalar(
            select(PonPort).where(PonPort.id == pon_port_id).with_for_update()
        )
        if not pon_port or not bool(getattr(pon_port, "is_active", True)):
            raise HTTPException(status_code=404, detail="PON port not found")
        if ont.olt_device_id and pon_port.olt_id != ont.olt_device_id:
            raise HTTPException(
                status_code=400,
                detail="PON port does not belong to the ONT's OLT",
            )
        if active and pon_port.max_ont_capacity is not None:
            assigned_count = (
                db.scalar(
                    select(func.count(OntAssignment.id))
                    .where(OntAssignment.pon_port_id == pon_port.id)
                    .where(OntAssignment.active.is_(True))
                )
                or 0
            )
            if assigned_count >= pon_port.max_ont_capacity:
                raise HTTPException(
                    status_code=409,
                    detail="PON port ONT capacity has been reached",
                )

    if active:
        stmt = (
            select(OntAssignment)
            .where(OntAssignment.ont_unit_id == ont.id)
            .where(OntAssignment.active.is_(True))
            .with_for_update()
            .limit(1)
        )
        if current_assignment_id is not None:
            stmt = stmt.where(OntAssignment.id != current_assignment_id)
        existing_active = db.scalars(stmt).first()
        if existing_active is not None:
            raise HTTPException(
                status_code=409,
                detail="ONT already has an active assignment",
            )

    return ont, pon_port


def _raise_assignment_conflict(exc: IntegrityError) -> None:
    message = str(getattr(exc, "orig", exc))
    if "ix_ont_assignments_active_unit" in message or "ont_assignments" in message:
        raise HTTPException(
            status_code=409,
            detail="ONT already has an active assignment",
        ) from exc
    raise exc


def _is_retryable_assignment_error(exc: OperationalError) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return any(
        token in message
        for token in (
            "deadlock detected",
            "could not serialize access",
            "lock timeout",
        )
    )


def _validate_assignment_customer_links(
    db: Session,
    *,
    subscriber_id: object | None,
    service_address_id: object | None,
) -> None:
    if subscriber_id is None:
        if service_address_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Service address requires a subscriber",
            )
        return
    network_validators.validate_cpe_device_links(
        db,
        str(subscriber_id),
        str(service_address_id) if service_address_id is not None else None,
    )


def _has_other_active_assignment(
    db: Session,
    *,
    ont_unit_id: object,
    exclude_assignment_id: object | None = None,
) -> bool:
    stmt = (
        select(OntAssignment.id)
        .where(OntAssignment.ont_unit_id == ont_unit_id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    )
    if exclude_assignment_id is not None:
        stmt = stmt.where(OntAssignment.id != exclude_assignment_id)
    return db.scalars(stmt).first() is not None


def _sync_ont_assignment_runtime(db: Session, ont: OntUnit) -> None:
    from app.services.network.cpe import ensure_cpe_for_ont

    has_active_assignment = _has_other_active_assignment(db, ont_unit_id=ont.id)
    ont.is_active = has_active_assignment
    ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)


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
    def create(cls, db: Session, payload) -> OLTDevice:
        device = super().create(db, payload)
        emit_event(
            db,
            EventType.olt_created,
            {"olt_id": str(device.id), "name": device.name},
            actor="system",
        )
        return device

    @classmethod
    def get(cls, db: Session, device_id: str) -> OLTDevice:
        return super().get(db, device_id)

    @classmethod
    def update(cls, db: Session, device_id: str, payload: OLTDeviceUpdate) -> OLTDevice:
        device = super().update(db, device_id, payload)
        emit_event(
            db,
            EventType.olt_updated,
            {"olt_id": str(device.id), "name": device.name},
            actor="system",
        )
        return device

    @classmethod
    def delete(cls, db: Session, device_id: str) -> None:
        device = cls.get(db, device_id)
        linked_onts = (
            db.scalar(
                select(func.count(OntUnit.id))
                .where(OntUnit.olt_device_id == device.id)
                .where(OntUnit.is_active.is_(True))
            )
            or 0
        )
        active_assignments = (
            db.scalar(
                select(func.count(OntAssignment.id))
                .join(PonPort, OntAssignment.pon_port_id == PonPort.id)
                .where(PonPort.olt_id == device.id)
                .where(OntAssignment.active.is_(True))
            )
            or 0
        )
        if linked_onts or active_assignments:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot delete OLT while active ONTs or assignments exist. "
                    "Return ONTs to inventory or deactivate assignments first."
                ),
            )
        emit_event(
            db,
            EventType.olt_deleted,
            {"olt_id": str(device.id), "name": device.name},
            actor="system",
        )
        super().delete(db, device_id)

    @staticmethod
    def propagate_acs_to_onts(db: Session, olt_id: str) -> dict[str, int]:
        """Propagate OLT's ACS server to all its unbound ONTs.

        Returns stats dict with updated, already_bound, total counts.
        """
        olt = db.get(OLTDevice, olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")

        acs_id = getattr(olt, "tr069_acs_server_id", None)
        if not acs_id:
            raise HTTPException(
                status_code=400,
                detail="OLT has no ACS server configured",
            )

        onts = list(
            db.scalars(select(OntUnit).where(OntUnit.olt_device_id == olt.id)).all()
        )
        total = len(onts)
        updated = 0
        already_bound = 0
        for ont in onts:
            if getattr(ont, "tr069_acs_server_id", None) == acs_id:
                already_bound += 1
            else:
                ont.tr069_acs_server_id = acs_id
                updated += 1
        if updated:
            db.commit()
        return {"updated": updated, "already_bound": already_bound, "total": total}

    @staticmethod
    def backfill_pon_ports(db: Session, olt_id: str) -> dict[str, int]:
        """Create missing PON ports from ONT board/port data and link assignments.

        Returns stats dict with ports_created, assignments_linked, total_onts.
        """
        olt = db.get(OLTDevice, olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")

        onts = list(
            db.scalars(select(OntUnit).where(OntUnit.olt_device_id == olt.id)).all()
        )
        total_onts = len(onts)
        ports_created = 0
        assignments_linked = 0

        existing_ports: dict[str, PonPort] = {}
        for port in db.scalars(select(PonPort).where(PonPort.olt_id == olt.id)).all():
            key = f"{getattr(port, 'board', '')}/{getattr(port, 'port', '')}"
            existing_ports[key] = port

        for ont in onts:
            board = getattr(ont, "board", None)
            port_str = getattr(ont, "port", None)
            if not board or not port_str:
                continue
            key = f"{board}/{port_str}"
            if key not in existing_ports:
                import uuid as _uuid

                new_port = PonPort(
                    id=str(_uuid.uuid4()),
                    olt_id=str(olt.id),
                    board=board,
                    port=port_str,
                    label=f"{board}/{port_str}",
                    is_active=True,
                )
                db.add(new_port)
                db.flush()
                existing_ports[key] = new_port
                ports_created += 1

        if ports_created:
            db.commit()
        return {
            "ports_created": ports_created,
            "assignments_linked": assignments_linked,
            "total_onts": total_onts,
        }


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
            card = db.get(OltCard, card_port.card_id)
            shelf = db.get(OltShelf, card.shelf_id) if card else None
            if not shelf or shelf.olt_id != payload.olt_id:
                raise HTTPException(
                    status_code=400,
                    detail="OLT card port does not belong to the selected OLT",
                )
        elif payload.card_id:
            if payload.port_number is None:
                raise HTTPException(
                    status_code=400,
                    detail="port_number is required when card_id is provided",
                )
            card = db.get(OltCard, payload.card_id)
            if not card:
                raise HTTPException(status_code=404, detail="OLT card not found")
            shelf = db.get(OltShelf, card.shelf_id)
            if not shelf or shelf.olt_id != payload.olt_id:
                raise HTTPException(
                    status_code=400,
                    detail="OLT card does not belong to the selected OLT",
                )
            card_port = db.scalars(
                select(OltCardPort)
                .where(OltCardPort.card_id == payload.card_id)
                .where(OltCardPort.port_number == payload.port_number)
                .limit(1)
            ).first()
            if card_port is None:
                card_port = OltCardPort(
                    card_id=payload.card_id,
                    port_number=payload.port_number,
                    port_type=OltPortType.pon,
                    is_active=True,
                )
                db.add(card_port)
                db.flush()
        data = payload.model_dump()
        if not card_port:
            parsed_name = _parse_canonical_pon_name(data.get("name"))
            if parsed_name is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Canonical frame/slot/port name is required when no OLT card "
                        "or card port is linked"
                    ),
                )
            _, parsed_port_number = parsed_name
            data["name"] = str(data["name"]).strip()
            data["port_number"] = parsed_port_number
            existing_port = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == payload.olt_id)
                .where(PonPort.name == data["name"])
                .limit(1)
            ).first()
            if existing_port is not None:
                existing_port.is_active = True
                existing_port.port_number = parsed_port_number
                if data.get("notes") is not None:
                    existing_port.notes = data["notes"]
                if data.get("max_ont_capacity") is not None:
                    existing_port.max_ont_capacity = data["max_ont_capacity"]
                db.commit()
                db.refresh(existing_port)
                return existing_port
        if payload.card_id and not payload.olt_card_port_id and card_port:
            data["olt_card_port_id"] = card_port.id
        if card_port:
            canonical_name = _canonical_pon_name_from_card_port(db, card_port)
            card_port.name = canonical_name
            card_port.is_active = True
            data["port_number"] = card_port.port_number
            data["name"] = canonical_name
            existing_port = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == payload.olt_id)
                .where(PonPort.olt_card_port_id == card_port.id)
                .limit(1)
            ).first()
            if existing_port is None:
                existing_port = db.scalars(
                    select(PonPort)
                    .where(PonPort.olt_id == payload.olt_id)
                    .where(PonPort.name == canonical_name)
                    .limit(1)
                ).first()
            if existing_port is not None:
                existing_port.olt_card_port_id = card_port.id
                existing_port.port_number = card_port.port_number
                existing_port.name = canonical_name
                existing_port.is_active = True
                if data.get("notes") is not None:
                    existing_port.notes = data["notes"]
                if data.get("max_ont_capacity") is not None:
                    existing_port.max_ont_capacity = data["max_ont_capacity"]
                db.commit()
                db.refresh(existing_port)
                return existing_port
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
        target_olt_id = data.get("olt_id", port.olt_id)
        if "olt_id" in data:
            olt = db.get(OLTDevice, target_olt_id)
            if not olt:
                raise HTTPException(status_code=404, detail="OLT device not found")
        target_card_port_id = data.get("olt_card_port_id", port.olt_card_port_id)
        if target_card_port_id:
            card_port = db.get(OltCardPort, target_card_port_id)
            if not card_port:
                raise HTTPException(status_code=404, detail="OLT card port not found")
            card = db.get(OltCard, card_port.card_id)
            shelf = db.get(OltShelf, card.shelf_id) if card else None
            if not shelf or shelf.olt_id != target_olt_id:
                raise HTTPException(
                    status_code=400,
                    detail="OLT card port does not belong to the selected OLT",
                )
            canonical_name = _canonical_pon_name_from_card_port(db, card_port)
            card_port.name = canonical_name
            card_port.is_active = True
            data["port_number"] = card_port.port_number
            data["name"] = canonical_name
            duplicate_by_name = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == target_olt_id)
                .where(PonPort.name == canonical_name)
                .where(PonPort.id != port.id)
                .limit(1)
            ).first()
            if duplicate_by_name is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A PON port already exists for this OLT and name",
                )
            duplicate = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == target_olt_id)
                .where(PonPort.olt_card_port_id == target_card_port_id)
                .where(PonPort.id != port.id)
                .limit(1)
            ).first()
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A PON port already exists for this OLT card port",
                )
        else:
            target_name = data.get("name", port.name)
            parsed_name = _parse_canonical_pon_name(target_name)
            if parsed_name is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Canonical frame/slot/port name is required when no OLT card "
                        "or card port is linked"
                    ),
                )
            _, parsed_port_number = parsed_name
            data["name"] = str(target_name).strip()
            data["port_number"] = parsed_port_number
            duplicate = db.scalars(
                select(PonPort)
                .where(PonPort.olt_id == target_olt_id)
                .where(PonPort.name == data["name"])
                .where(PonPort.id != port.id)
                .limit(1)
            ).first()
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A PON port already exists for this OLT and name",
                )
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

        assigned_stmt = select(
            func.count(func.distinct(OntAssignment.pon_port_id))
        ).where(OntAssignment.active.is_(True))
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
        stmt = select(OntUnit).options(*_ONT_STATUS_LOADS)
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
        pon_hint: str | None = None,
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

        stmt = select(OntUnit).options(*_ONT_STATUS_LOADS)

        # Filter by OLT or PON port via active assignment join
        if pon_port_id:
            stmt = stmt.join(
                OntAssignment,
                (OntAssignment.ont_unit_id == OntUnit.id)
                & (OntAssignment.active.is_(True)),
            )
            stmt = stmt.where(OntAssignment.pon_port_id == coerce_uuid(pon_port_id))
        elif olt_id:
            # Include ONTs linked by assignment->pon_port and ONTs directly linked to OLT.
            stmt = stmt.outerjoin(
                OntAssignment,
                (OntAssignment.ont_unit_id == OntUnit.id)
                & (OntAssignment.active.is_(True)),
            ).outerjoin(PonPort, PonPort.id == OntAssignment.pon_port_id)
            olt_uuid = coerce_uuid(olt_id)
            stmt = stmt.where(
                or_(
                    PonPort.olt_id == olt_uuid,
                    OntUnit.olt_device_id == olt_uuid,
                )
            )

        # Optional hint for SNMP-only PON rows (e.g., "0/2/7")
        if pon_hint:
            like_hint = f"%{pon_hint.strip()}%"
            combined = func.concat(
                func.coalesce(OntUnit.board, ""),
                "/",
                func.coalesce(OntUnit.port, ""),
            )
            stmt = stmt.where(
                or_(
                    OntUnit.board.ilike(like_hint),
                    OntUnit.port.ilike(like_hint),
                    combined.ilike(like_hint),
                )
            )

        # Filter by zone
        if zone_id:
            stmt = stmt.where(OntUnit.zone_id == coerce_uuid(zone_id))

        # Filter by active state
        stmt = apply_active_state(stmt, OntUnit.is_active, is_active)

        # Filter by online status
        from app.models.network import OnuOnlineStatus

        if online_status and online_status in ("online", "offline", "unknown"):
            stmt = stmt.where(
                OntUnit.effective_status == OnuOnlineStatus(online_status)
            )

        # Filter by vendor
        if vendor:
            stmt = stmt.where(OntUnit.vendor.ilike(f"%{vendor}%"))

        # Broader text search for ONT operations workflows.
        if search:
            term = f"%{search.strip()}%"
            search_assignment = aliased(OntAssignment)
            search_pon_port = aliased(PonPort)
            search_olt = aliased(OLTDevice)
            search_subscriber = aliased(Subscriber)

            # Build list of serial search conditions including hex serial variants
            serial_conditions = [OntUnit.serial_number.ilike(term)]

            # If search looks like a hex serial, also search for the decoded form
            search_clean = search.strip().upper()
            decoded = decode_huawei_hex_serial(search_clean)
            if decoded:
                serial_conditions.append(OntUnit.serial_number.ilike(f"%{decoded}%"))

            # If search looks like a vendor+serial, also search for the hex form
            encoded = encode_to_hex_serial(search_clean)
            if encoded:
                serial_conditions.append(OntUnit.serial_number.ilike(f"%{encoded}%"))

            stmt = (
                stmt.outerjoin(
                    search_assignment,
                    (search_assignment.ont_unit_id == OntUnit.id)
                    & (search_assignment.active.is_(True)),
                )
                .outerjoin(
                    search_pon_port, search_pon_port.id == search_assignment.pon_port_id
                )
                .outerjoin(search_olt, search_olt.id == search_pon_port.olt_id)
                .outerjoin(
                    search_subscriber,
                    search_subscriber.id == search_assignment.subscriber_id,
                )
                .where(
                    or_(
                        *serial_conditions,
                        OntUnit.mac_address.ilike(term),
                        OntUnit.vendor.ilike(term),
                        OntUnit.model.ilike(term),
                        OntUnit.firmware_version.ilike(term),
                        OntUnit.notes.ilike(term),
                        OntUnit.board.ilike(term),
                        OntUnit.port.ilike(term),
                        search_olt.name.ilike(term),
                        search_olt.hostname.ilike(term),
                        search_pon_port.name.ilike(term),
                        search_pon_port.notes.ilike(term),
                        search_subscriber.display_name.ilike(term),
                        search_subscriber.subscriber_number.ilike(term),
                        search_subscriber.email.ilike(term),
                    )
                )
            )

        # Filter by signal quality using thresholds
        if signal_quality and signal_quality in ("good", "warning", "critical"):
            warn, crit = get_signal_thresholds(db)
            if signal_quality == "critical":
                stmt = stmt.where(OntUnit.olt_rx_signal_dbm < crit)
            elif signal_quality == "warning":
                stmt = stmt.where(
                    and_(
                        OntUnit.olt_rx_signal_dbm >= crit,
                        OntUnit.olt_rx_signal_dbm < warn,
                    )
                )
            elif signal_quality == "good":
                stmt = stmt.where(OntUnit.olt_rx_signal_dbm >= warn)

        # Count before pagination (use subquery to handle JOINs correctly)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = db.scalar(count_stmt) or 0

        # Ordering — include signal-based sorting with nulls last so diagnostics
        # does not prioritize devices missing telemetry over real low-signal rows.
        if order_by == "signal":
            if order_dir == "desc":
                stmt = stmt.order_by(
                    OntUnit.olt_rx_signal_dbm.is_(None),
                    OntUnit.olt_rx_signal_dbm.desc(),
                )
            else:
                stmt = stmt.order_by(
                    OntUnit.olt_rx_signal_dbm.is_(None),
                    OntUnit.olt_rx_signal_dbm.asc(),
                )
        else:
            allowed = {
                "serial_number": OntUnit.serial_number,
                "created_at": OntUnit.created_at,
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

    @classmethod
    def create(cls, db: Session, payload: OntAssignmentCreate) -> OntAssignment:
        ont, _pon_port = _validate_assignment_target(
            db,
            ont_unit_id=payload.ont_unit_id,
            pon_port_id=payload.pon_port_id,
            active=payload.active,
        )
        _validate_assignment_customer_links(
            db,
            subscriber_id=payload.subscriber_id,
            service_address_id=payload.service_address_id,
        )
        assignment = OntAssignment(**payload.model_dump())
        from app.services.network.cpe import ensure_cpe_for_ont

        for attempt in range(3):
            try:
                with db.begin_nested():
                    db.add(assignment)
                    db.flush()
                    if assignment.active:
                        ont.is_active = True
                        ensure_cpe_for_ont(db, ont, assignment, commit=False)
                    else:
                        _sync_ont_assignment_runtime(db, ont)
                db.commit()
                break
            except IntegrityError as exc:
                db.rollback()
                _raise_assignment_conflict(exc)
            except OperationalError as exc:
                db.rollback()
                if attempt >= 2 or not _is_retryable_assignment_error(exc):
                    raise
                sleep(0.05 * (2**attempt))
            except Exception:
                db.expire(ont)
                raise
        db.refresh(assignment)
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
    def update(
        cls, db: Session, assignment_id: str, payload: OntAssignmentUpdate
    ) -> OntAssignment:
        assignment = cls.get(db, assignment_id)
        original_ont_unit_id = assignment.ont_unit_id
        data = payload.model_dump(exclude_unset=True)
        fields_set = set(payload.model_fields_set)
        target_ont_unit_id = data.get("ont_unit_id", assignment.ont_unit_id)
        target_pon_port_id = data.get("pon_port_id", assignment.pon_port_id)
        target_active = data.get("active", assignment.active)
        target_subscriber_id = (
            data.get("subscriber_id")
            if "subscriber_id" in fields_set
            else assignment.subscriber_id
        )
        target_service_address_id = (
            data.get("service_address_id")
            if "service_address_id" in fields_set
            else assignment.service_address_id
        )

        ont, _pon_port = _validate_assignment_target(
            db,
            ont_unit_id=target_ont_unit_id,
            pon_port_id=target_pon_port_id,
            active=bool(target_active),
            current_assignment_id=assignment.id,
        )
        _validate_assignment_customer_links(
            db,
            subscriber_id=target_subscriber_id,
            service_address_id=target_service_address_id,
        )

        original_ont = (
            ont
            if original_ont_unit_id == ont.id
            else db.get(OntUnit, original_ont_unit_id)
        )

        for attempt in range(3):
            try:
                with db.begin_nested():
                    for key, value in data.items():
                        setattr(assignment, key, value)
                    db.flush()

                    if assignment.active:
                        ont.is_active = True
                    else:
                        _sync_ont_assignment_runtime(db, ont)

                    if original_ont is not None and original_ont.id != ont.id:
                        _sync_ont_assignment_runtime(db, original_ont)

                    if assignment.active:
                        from app.services.network.cpe import ensure_cpe_for_ont

                        ensure_cpe_for_ont(db, ont, assignment, commit=False)
                db.commit()
                break
            except IntegrityError as exc:
                db.rollback()
                _raise_assignment_conflict(exc)
            except OperationalError as exc:
                db.rollback()
                if attempt >= 2 or not _is_retryable_assignment_error(exc):
                    raise
                sleep(0.05 * (2**attempt))
            except Exception:
                if original_ont is not None:
                    db.expire(original_ont)
                if ont is not original_ont:
                    db.expire(ont)
                raise
        db.refresh(assignment)
        return assignment

    @classmethod
    def delete(cls, db: Session, assignment_id: str) -> None:
        assignment = cls.get(db, assignment_id)
        ont = db.get(OntUnit, assignment.ont_unit_id)
        try:
            with db.begin_nested():
                db.delete(assignment)
                db.flush()
                if ont is not None:
                    _sync_ont_assignment_runtime(db, ont)
            db.commit()
        except Exception:
            if ont is not None:
                db.expire(ont)
            raise


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
    def update(
        cls, db: Session, unit_id: str, payload: OltPowerUnitUpdate
    ) -> OltPowerUnit:
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
    def update(
        cls, db: Session, module_id: str, payload: OltSfpModuleUpdate
    ) -> OltSfpModule:
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
