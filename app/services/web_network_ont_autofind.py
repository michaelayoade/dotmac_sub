"""Global persisted OLT autofind inventory helpers."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, OntUnit
from app.models.ont_autofind import OltAutofindCandidate
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network.olt_web_audit import log_olt_audit_event

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

    stats = sync_olt_autofind_entries(db, olt_id=olt_id, entries=entries)
    return True, message, stats


def sync_olt_autofind_entries(
    db: Session,
    *,
    olt_id: str,
    entries: Iterable[object],
) -> dict[str, int]:
    """Persist OLT autofind entries already read from an OLT."""
    olt = db.get(OLTDevice, olt_id)
    if not olt:
        return {}

    entry_list = list(entries)
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

    for entry in entry_list:
        serial_number = str(getattr(entry, "serial_number", "") or "").strip()
        fsp = str(getattr(entry, "fsp", "") or "").strip()
        if not fsp or not serial_number:
            continue
        key = (fsp, _normalize_serial(serial_number))
        seen_keys.add(key)
        candidate = by_key.get(key)
        if candidate is None:
            candidate = db.scalars(
                select(OltAutofindCandidate).where(
                    OltAutofindCandidate.olt_id == olt.id,
                    OltAutofindCandidate.fsp == fsp,
                    OltAutofindCandidate.serial_number == serial_number,
                )
            ).first()

        matched_ont = _find_ont_by_serial(db, serial_number)
        if candidate is None:
            candidate = OltAutofindCandidate(
                olt_id=olt.id,
                ont_unit_id=matched_ont.id if matched_ont else None,
                fsp=fsp,
                serial_number=serial_number,
                serial_hex=getattr(entry, "serial_hex", None),
                vendor_id=getattr(entry, "vendor_id", None),
                model=getattr(entry, "model", None),
                software_version=getattr(entry, "software_version", None),
                mac=getattr(entry, "mac", None),
                equipment_sn=getattr(entry, "equipment_sn", None),
                autofind_time=getattr(entry, "autofind_time", None),
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
            candidate.serial_hex = getattr(entry, "serial_hex", None)
            candidate.vendor_id = getattr(entry, "vendor_id", None)
            candidate.model = getattr(entry, "model", None)
            candidate.software_version = getattr(entry, "software_version", None)
            candidate.mac = getattr(entry, "mac", None)
            candidate.equipment_sn = getattr(entry, "equipment_sn", None)
            candidate.autofind_time = getattr(entry, "autofind_time", None)
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
    return {
        "discovered": len(entry_list),
        "created": created,
        "updated": updated,
        "resolved": resolved,
    }


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


def restore_candidate(db: Session, *, candidate_id: str) -> tuple[bool, str]:
    """Restore a disappeared autofind candidate to active state.

    Args:
        db: Database session
        candidate_id: UUID of the autofind candidate

    Returns:
        Tuple of (success, message)
    """
    from app.services.common import coerce_uuid

    candidate = db.get(OltAutofindCandidate, coerce_uuid(candidate_id))
    if not candidate:
        return False, "Autofind candidate not found"
    if candidate.is_active:
        return False, "Candidate is already active"
    if candidate.resolution_reason != "disappeared":
        return False, f"Cannot restore candidate with resolution: {candidate.resolution_reason}"

    candidate.is_active = True
    candidate.resolution_reason = None
    candidate.resolved_at = None
    candidate.last_seen_at = datetime.now(UTC)
    db.commit()
    logger.info(
        "autofind_candidate_restored",
        extra={
            "event": "autofind_candidate_restored",
            "candidate_id": str(candidate.id),
            "serial_number": candidate.serial_number,
            "olt_id": str(candidate.olt_id),
        },
    )
    return True, f"Restored autofind candidate {candidate.serial_number}"


def restore_candidate_audited(
    db: Session, *, candidate_id: str, request: Request | None = None
) -> tuple[bool, str]:
    ok, message = restore_candidate(db, candidate_id=candidate_id)
    status = "success" if ok else "error"
    log_olt_audit_event(
        db,
        request=request,
        action="restore_autofind_candidate",
        entity_type="olt_autofind_candidate",
        entity_id=candidate_id,
        metadata={"result": status, "message": message},
        status_code=200 if ok else 400,
        is_success=ok,
    )
    return ok, message


def build_unconfigured_onts_page_data(
    db: Session,
    *,
    search: str | None = None,
    olt_id: str | None = None,
    view: str | None = None,
    resolution: str | None = None,
) -> dict[str, object]:
    """Build page data for the global unconfigured ONT inventory."""
    selected_view = (view or "active").strip().lower()
    if selected_view not in {"active", "history", "all"}:
        selected_view = "active"
    selected_resolution = (resolution or "").strip().lower()
    if selected_resolution not in {"authorized", "disappeared"}:
        selected_resolution = ""

    query = (
        db.query(OltAutofindCandidate, OLTDevice)
        .join(OLTDevice, OLTDevice.id == OltAutofindCandidate.olt_id)
    )
    if selected_view == "active":
        query = query.filter(OltAutofindCandidate.is_active.is_(True))
    elif selected_view == "history":
        query = query.filter(OltAutofindCandidate.is_active.is_(False))
    if selected_resolution:
        query = query.filter(OltAutofindCandidate.resolution_reason == selected_resolution)
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
                OltAutofindCandidate.resolution_reason.ilike(term),
            )
        )

    rows = query.order_by(
        func.coalesce(
            OltAutofindCandidate.resolved_at, OltAutofindCandidate.last_seen_at
        ).desc(),
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
            "is_active": candidate.is_active,
            "resolution_reason": candidate.resolution_reason,
            "resolved_at": candidate.resolved_at,
            "notes": candidate.notes,
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
    history_total = (
        db.scalar(
            select(func.count())
            .select_from(OltAutofindCandidate)
            .where(OltAutofindCandidate.is_active.is_(False))
        )
        or 0
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
        "selected_view": selected_view,
        "selected_resolution": selected_resolution,
        "olts": olts,
        "stats": {
            "active_candidates": int(active_total),
            "history_candidates": int(history_total),
            "olts_with_candidates": len({entry["olt_id"] for entry in entries}),
            "last_seen_at": last_seen,
        },
    }
