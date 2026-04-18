"""OLT autofind inventory helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import (
    GponChannel,
    OLTDevice,
    OntUnit,
    OnuOnlineStatus,
    PonType,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.services import tr069 as tr069_service
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_assignment_alignment import (
    align_ont_assignment_to_authoritative_fsp,
)
from app.services.web_network_ont_autofind import _find_ont_by_serial


def parse_fsp_parts(fsp: str) -> tuple[str | None, str | None]:
    """Split an F/S/P string into board and port fragments."""
    parts = [part.strip() for part in str(fsp or "").split("/") if part.strip()]
    if len(parts) != 3:
        return None, None
    return f"{parts[0]}/{parts[1]}", parts[2]


def persist_authorized_ont_inventory(
    db: Session,
    *,
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    ont_id: int,
) -> None:
    """Persist ONT + assignment state after OLT-side authorization."""
    normalized_serial = str(serial_number or "").strip()
    board, port = parse_fsp_parts(fsp)
    if not normalized_serial or not board or not port:
        return

    now = datetime.now(UTC)
    candidate = db.scalars(
        select(OltAutofindCandidate)
        .where(
            OltAutofindCandidate.olt_id == olt.id,
            OltAutofindCandidate.fsp == fsp,
            OltAutofindCandidate.serial_number == normalized_serial,
        )
        .order_by(
            OltAutofindCandidate.updated_at.desc(),
            OltAutofindCandidate.created_at.desc(),
        )
    ).first()

    ont = _find_ont_by_serial(db, normalized_serial)
    if ont is None:
        ont = OntUnit(
            serial_number=normalized_serial,
            is_active=True,
        )
        db.add(ont)
    else:
        # Lock existing ONT before modification to prevent concurrent updates
        ont = db.scalars(
            select(OntUnit).where(OntUnit.id == ont.id).with_for_update()
        ).first()
        if ont is None:
            return  # ONT was deleted concurrently

    ont.is_active = True
    ont.olt_device_id = olt.id
    ont.pon_type = PonType.gpon
    ont.gpon_channel = GponChannel.gpon
    ont.board = board
    ont.port = port
    ont.external_id = str(ont_id)
    ont.online_status = OnuOnlineStatus.unknown
    ont.tr069_acs_server_id = olt.tr069_acs_server_id
    ont.last_sync_source = "olt_ssh_authorize"
    ont.last_sync_at = now
    tr069_service.sync_ont_acs_server(db, ont, olt.tr069_acs_server_id)

    if candidate is not None:
        if candidate.vendor_id:
            ont.vendor = candidate.vendor_id
        if candidate.model:
            ont.model = candidate.model
        if candidate.software_version:
            ont.firmware_version = candidate.software_version
        if candidate.mac:
            ont.mac_address = candidate.mac

    align_ont_assignment_to_authoritative_fsp(
        db,
        ont=ont,
        olt_id=olt.id,
        fsp=fsp,
        assigned_at=now,
    )

    if candidate is not None:
        candidate.ont_unit = ont
        candidate.is_active = False
        candidate.resolution_reason = "authorized"
        candidate.resolved_at = now

    db.commit()


def get_autofind_onts(
    db: Session, olt_id: str
) -> tuple[bool, str, list[olt_ssh_service.AutofindEntry]]:
    """Retrieve unregistered ONTs from an OLT's autofind table via SSH."""
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", []
    return olt_ssh_service.get_autofind_onts(olt)


def get_autofind_onts_audited(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[bool, str, list[olt_ssh_service.AutofindEntry]]:
    ok, message, entries = get_autofind_onts(db, olt_id)
    log_olt_audit_event(
        db,
        request=request,
        action="autofind_scan",
        entity_id=olt_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "count": len(entries),
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message, entries
