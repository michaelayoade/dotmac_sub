"""Global persisted OLT autofind inventory helpers."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit
from app.models.ont_autofind import OltAutofindCandidate
from app.services.network import olt_ssh as olt_ssh_service

logger = logging.getLogger(__name__)


def _normalize_serial(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "").strip()).upper()


def _normalized_serial_expr(column):  # type: ignore[no-untyped-def]
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _find_ont_by_serial(db: Session, serial_number: str | None) -> OntUnit | None:
    normalized = _normalize_serial(serial_number)
    if not normalized:
        return None
    stmt = (
        select(OntUnit)
        .where(_normalized_serial_expr(OntUnit.serial_number) == normalized)
        .where(OntUnit.is_active.is_(True))
        .order_by(OntUnit.updated_at.desc(), OntUnit.created_at.desc())
    )
    return db.scalars(stmt).first()


def sync_olt_autofind_candidates(
    db: Session,
    olt_id: str,
) -> tuple[bool, str, dict[str, int]]:
    """Refresh cached autofind candidates for a single OLT."""
    olt = db.get(OLTDevice, olt_id)
    if not olt:
        return False, "OLT not found", {}

    ok, message, entries = olt_ssh_service.get_autofind_onts(olt)
    if not ok:
        return False, message, {}

    now = datetime.now(UTC)
    active_entries = list(
        db.scalars(
            select(OltAutofindCandidate).where(
                OltAutofindCandidate.olt_id == olt.id,
                OltAutofindCandidate.is_active.is_(True),
            )
        ).all()
    )
    by_key = {
        (item.fsp, _normalize_serial(item.serial_number)): item
        for item in active_entries
    }
    seen_keys: set[tuple[str, str]] = set()
    created = 0
    updated = 0
    resolved = 0

    for entry in entries:
        serial_number = str(entry.serial_number or "").strip()
        key = (entry.fsp, _normalize_serial(serial_number))
        seen_keys.add(key)
        candidate = by_key.get(key)
        if candidate is None:
            candidate = db.scalars(
                select(OltAutofindCandidate).where(
                    OltAutofindCandidate.olt_id == olt.id,
                    OltAutofindCandidate.fsp == entry.fsp,
                    OltAutofindCandidate.serial_number == serial_number,
                )
            ).first()

        matched_ont = _find_ont_by_serial(db, serial_number)
        if candidate is None:
            candidate = OltAutofindCandidate(
                olt_id=olt.id,
                ont_unit_id=matched_ont.id if matched_ont else None,
                fsp=entry.fsp,
                serial_number=serial_number,
                serial_hex=entry.serial_hex,
                vendor_id=entry.vendor_id,
                model=entry.model,
                software_version=entry.software_version,
                mac=entry.mac,
                equipment_sn=entry.equipment_sn,
                autofind_time=entry.autofind_time,
                is_active=True,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(candidate)
            created += 1
        else:
            candidate.ont_unit_id = (
                matched_ont.id if matched_ont else candidate.ont_unit_id
            )
            candidate.serial_hex = entry.serial_hex
            candidate.vendor_id = entry.vendor_id
            candidate.model = entry.model
            candidate.software_version = entry.software_version
            candidate.mac = entry.mac
            candidate.equipment_sn = entry.equipment_sn
            candidate.autofind_time = entry.autofind_time
            candidate.is_active = True
            candidate.resolution_reason = None
            candidate.resolved_at = None
            candidate.last_seen_at = now
            updated += 1

    for candidate in active_entries:
        key = (candidate.fsp, _normalize_serial(candidate.serial_number))
        if key in seen_keys:
            continue
        candidate.is_active = False
        candidate.resolution_reason = "disappeared"
        candidate.resolved_at = now
        resolved += 1

    db.commit()
    stats = {
        "discovered": len(entries),
        "created": created,
        "updated": updated,
        "resolved": resolved,
    }
    return True, message, stats


def resolve_candidate_authorized(
    db: Session,
    *,
    olt_id: str,
    fsp: str,
    serial_number: str,
) -> None:
    """Mark a cached autofind candidate as authorized/resolved."""
    candidate = db.scalars(
        select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.fsp == fsp,
            OltAutofindCandidate.serial_number == serial_number,
        )
    ).first()
    if not candidate:
        return
    candidate.is_active = False
    candidate.resolution_reason = "authorized"
    candidate.resolved_at = datetime.now(UTC)
    matched_ont = _find_ont_by_serial(db, serial_number)
    if matched_ont:
        candidate.ont_unit_id = matched_ont.id
    db.commit()


def build_unconfigured_onts_page_data(
    db: Session,
    *,
    search: str | None = None,
    olt_id: str | None = None,
) -> dict[str, object]:
    """Build page data for the global unconfigured ONT inventory."""
    query = (
        db.query(OltAutofindCandidate, OLTDevice)
        .join(OLTDevice, OLTDevice.id == OltAutofindCandidate.olt_id)
        .filter(OltAutofindCandidate.is_active.is_(True))
    )
    if olt_id:
        query = query.filter(OltAutofindCandidate.olt_id == olt_id)
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                OltAutofindCandidate.serial_number.ilike(term),
                OltAutofindCandidate.fsp.ilike(term),
                OltAutofindCandidate.model.ilike(term),
                OltAutofindCandidate.mac.ilike(term),
                OLTDevice.name.ilike(term),
            )
        )

    rows = query.order_by(
        OltAutofindCandidate.last_seen_at.desc(),
        OLTDevice.name.asc(),
        OltAutofindCandidate.fsp.asc(),
    ).all()
    entries = [
        {
            "id": str(candidate.id),
            "olt_id": str(olt.id),
            "olt_name": olt.name,
            "fsp": candidate.fsp,
            "serial_number": candidate.serial_number,
            "vendor_id": candidate.vendor_id,
            "model": candidate.model,
            "mac": candidate.mac,
            "software_version": candidate.software_version,
            "autofind_time": candidate.autofind_time,
            "first_seen_at": candidate.first_seen_at,
            "last_seen_at": candidate.last_seen_at,
        }
        for candidate, olt in rows
    ]
    active_total = (
        db.scalar(
            select(func.count())
            .select_from(OltAutofindCandidate)
            .where(OltAutofindCandidate.is_active.is_(True))
        )
        or 0
    )
    last_seen = db.scalar(
        select(func.max(OltAutofindCandidate.last_seen_at)).where(
            OltAutofindCandidate.is_active.is_(True)
        )
    )
    olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name.asc())
        ).all()
    )
    return {
        "entries": entries,
        "search": search or "",
        "selected_olt_id": olt_id or "",
        "olts": olts,
        "stats": {
            "active_candidates": int(active_total),
            "olts_with_candidates": len({entry["olt_id"] for entry in entries}),
            "last_seen_at": last_seen,
        },
    }
