"""CRUD manager for ONT units."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
)
from app.schemas.network import OntUnitUpdate
from app.services.common import coerce_uuid
from app.services.crud import CRUDManager
from app.services.network._common import (
    SubscriberValidator,
    _apply_ordering,
    _apply_pagination,
    decode_huawei_hex_serial,
    encode_to_hex_serial,
)
from app.services.network.olt_crud_common import parse_canonical_pon_name
from app.services.query_builders import apply_active_state

_ONT_STATUS_LOADS = (
    joinedload(OntUnit.tr069_acs_server),
    joinedload(OntUnit.olt_device).joinedload(OLTDevice.tr069_acs_server),
)


class OntUnits(CRUDManager[OntUnit]):
    model = OntUnit
    not_found_detail = "ONT unit not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    def __init__(self, subscriber_validator: SubscriberValidator | None = None) -> None:
        self._subscriber_validator = subscriber_validator

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

    def list_advanced(
        self,
        db: Session,
        *,
        olt_id: str | None = None,
        pon_port_id: str | None = None,
        pon_hint: str | None = None,
        zone_id: str | None = None,
        signal_quality: str | None = None,
        olt_status: str | None = None,
        authorization_status: str | None = None,
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
        from app.services.network.signal_thresholds import get_signal_thresholds

        stmt = select(OntUnit).options(*_ONT_STATUS_LOADS)

        if pon_port_id:
            pon_uuid = coerce_uuid(pon_port_id)
            pon_port = db.get(PonPort, pon_uuid)
            stmt = stmt.outerjoin(
                OntAssignment,
                (OntAssignment.ont_unit_id == OntUnit.id)
                & (OntAssignment.active.is_(True)),
            )
            pon_conditions: list[Any] = [OntAssignment.pon_port_id == pon_uuid]
            if pon_port is not None:
                parsed = parse_canonical_pon_name(pon_port.name)
                if parsed:
                    board, port_number = parsed
                    pon_conditions.append(
                        and_(
                            OntUnit.olt_device_id == pon_port.olt_id,
                            OntUnit.board == board,
                            OntUnit.port == str(port_number),
                        )
                    )
            stmt = stmt.where(or_(*pon_conditions))
        elif olt_id:
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

        if pon_hint:
            like_hint = f"%{pon_hint.strip()}%"
            combined = (
                func.coalesce(OntUnit.board, "") + "/" + func.coalesce(OntUnit.port, "")
            )
            stmt = stmt.where(
                or_(
                    OntUnit.board.ilike(like_hint),
                    OntUnit.port.ilike(like_hint),
                    combined.ilike(like_hint),
                )
            )

        if zone_id:
            stmt = stmt.where(OntUnit.zone_id == coerce_uuid(zone_id))

        stmt = apply_active_state(stmt, OntUnit.is_active, is_active)

        from app.models.network import OntAuthorizationStatus

        if authorization_status:
            normalized_auth = authorization_status.strip().lower()
            if normalized_auth == "authorized":
                active_assignment = aliased(OntAssignment)
                has_active_assignment = exists(
                    select(1)
                    .select_from(active_assignment)
                    .where(
                        active_assignment.ont_unit_id == OntUnit.id,
                        active_assignment.active.is_(True),
                    )
                    .correlate(OntUnit)
                )
                stmt = stmt.where(
                    or_(
                        OntUnit.authorization_status
                        == OntAuthorizationStatus.authorized,
                        has_active_assignment,
                    )
                )
            elif normalized_auth == "unauthorized":
                stmt = stmt.where(
                    or_(
                        OntUnit.authorization_status.is_(None),
                        OntUnit.authorization_status
                        != OntAuthorizationStatus.authorized,
                    )
                )

        if vendor:
            stmt = stmt.where(OntUnit.vendor.ilike(f"%{vendor}%"))

        if search:
            term = f"%{search.strip()}%"
            search_assignment = aliased(OntAssignment)
            search_pon_port = aliased(PonPort)
            search_olt = aliased(OLTDevice)
            direct_olt = aliased(OLTDevice)
            direct_pon_port = aliased(PonPort)
            direct_pon_name = (
                func.coalesce(OntUnit.board, "") + "/" + func.coalesce(OntUnit.port, "")
            )

            serial_conditions = [OntUnit.serial_number.ilike(term)]

            search_clean = search.strip().upper()
            decoded = decode_huawei_hex_serial(search_clean)
            if decoded:
                serial_conditions.append(OntUnit.serial_number.ilike(f"%{decoded}%"))

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
                .outerjoin(direct_olt, direct_olt.id == OntUnit.olt_device_id)
                .outerjoin(
                    direct_pon_port,
                    (direct_pon_port.olt_id == OntUnit.olt_device_id)
                    & (direct_pon_port.name == direct_pon_name),
                )
            )

            subscriber_conditions: Sequence[Any] = ()
            if self._subscriber_validator is not None:
                stmt, subscriber_conditions = (
                    self._subscriber_validator.augment_ont_search(
                        stmt,
                        term,
                        assignment_alias=search_assignment,
                    )
                )

            stmt = stmt.where(
                or_(
                    *serial_conditions,
                    OntUnit.mac_address.ilike(term),
                    OntUnit.vendor.ilike(term),
                    OntUnit.model.ilike(term),
                    OntUnit.firmware_version.ilike(term),
                    OntUnit.notes.ilike(term),
                    OntUnit.board.ilike(term),
                    OntUnit.port.ilike(term),
                    direct_pon_name.ilike(term),
                    search_olt.name.ilike(term),
                    search_olt.hostname.ilike(term),
                    search_pon_port.name.ilike(term),
                    search_pon_port.notes.ilike(term),
                    direct_olt.name.ilike(term),
                    direct_olt.hostname.ilike(term),
                    direct_pon_port.name.ilike(term),
                    direct_pon_port.notes.ilike(term),
                    *subscriber_conditions,
                )
            )

        if olt_status in {"online", "offline"}:
            stmt = stmt.where(OntUnit.olt_status == OnuOnlineStatus(olt_status))

        if signal_quality in {"good", "warning", "critical"}:
            warn, crit = get_signal_thresholds(db)
            signal_col = OntUnit.olt_rx_signal_dbm
            stmt = (
                stmt.where(signal_col.isnot(None))
                .where(signal_col >= -50.0)
                .where(signal_col <= 10.0)
            )
            if signal_quality == "critical":
                stmt = stmt.where(signal_col < crit)
            elif signal_quality == "warning":
                stmt = stmt.where(signal_col >= crit).where(signal_col < warn)
            else:
                stmt = stmt.where(signal_col >= warn)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = db.scalar(count_stmt) or 0

        if order_by == "signal":
            signal_order = (
                OntUnit.olt_rx_signal_dbm.desc()
                if order_dir == "desc"
                else OntUnit.olt_rx_signal_dbm.asc()
            )
            stmt = stmt.order_by(signal_order.nulls_last(), OntUnit.serial_number.asc())
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
    def update(cls, db: Session, unit_id: str, payload: OntUnitUpdate) -> OntUnit:  # type: ignore[override]
        return super().update(db, unit_id, payload)

    @classmethod
    def delete(cls, db: Session, unit_id: str) -> None:  # type: ignore[override]
        return super().delete(db, unit_id)
