"""Global persisted OLT autofind inventory helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import quote_plus

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, OntUnit
from app.models.ont_autofind import OltAutofindCandidate
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.serial_utils import normalize as normalize_serial
from app.services.network.serial_utils import (
    search_candidates as serial_search_candidates,
)

logger = logging.getLogger(__name__)


def _entry_serial(entry: object) -> str:
    serial_number = str(getattr(entry, "serial_number", "") or "").strip()
    if serial_number:
        return serial_number
    return str(getattr(entry, "serial_hex", "") or "").strip()


def _normalize_serial(value: str | None) -> str:
    return normalize_serial(value)


def _candidate_serial_values(candidate: OltAutofindCandidate) -> list[str]:
    values: list[str] = []
    for value in (candidate.serial_number, candidate.serial_hex):
        for serial in serial_search_candidates(value):
            normalized = normalize_serial(serial)
            if normalized and normalized not in values:
                values.append(normalized)
    return values


def _normalized_serial_expr(column):  # type: ignore[no-untyped-def]
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _find_ont_by_serial(db: Session, serial_number: str | None) -> OntUnit | None:
    candidates = [
        normalize_serial(candidate)
        for candidate in serial_search_candidates(serial_number)
    ]
    candidates = [candidate for candidate in dict.fromkeys(candidates) if candidate]
    if not candidates:
        return None
    stmt = (
        select(OntUnit)
        .where(_normalized_serial_expr(OntUnit.serial_number).in_(candidates))
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


def refresh_returned_ont_autofind(
    db: Session,
    *,
    olt_id: str | None,
    serial_number: str | None,
    fsp: str | None = None,
) -> dict[str, object]:
    """Refresh autofind after an ONT release and return list navigation context."""
    target_url = build_unconfigured_onts_redirect_url(
        search=serial_number or None,
        olt_id=olt_id or None,
    )
    if not olt_id:
        return {
            "ok": False,
            "message": "No previous OLT available for autofind refresh",
            "rediscovered": False,
            "url": target_url,
        }

    ok, message, stats = sync_olt_autofind_candidates(db, olt_id)
    rediscovered = False
    normalized_serial = _normalize_serial(serial_number)
    if ok and normalized_serial:
        query = select(OltAutofindCandidate).where(
            OltAutofindCandidate.olt_id == olt_id,
            OltAutofindCandidate.is_active.is_(True),
        )
        if fsp:
            query = query.where(OltAutofindCandidate.fsp == fsp)
        for candidate in db.scalars(query).all():
            if normalized_serial in _candidate_serial_values(candidate):
                rediscovered = True
                break

    return {
        "ok": ok,
        "message": message,
        "stats": stats,
        "rediscovered": rediscovered,
        "url": target_url,
    }


def scan_olt_autofind_results_context(
    db: Session,
    olt_id: str,
    *,
    request: Request | None = None,
) -> dict[str, object]:
    """Scan an OLT, persist autofind entries, and return template context."""
    from app.services.network import olt_autofind as olt_autofind_service

    ok, message, entries = olt_autofind_service.get_autofind_onts_audited(
        db, olt_id, request=request
    )
    if ok:
        sync_olt_autofind_entries(db, olt_id=olt_id, entries=entries)
    return {
        "olt_id": olt_id,
        "autofind_ok": ok,
        "autofind_message": message,
        "autofind_entries": [
            {
                "fsp": getattr(entry, "fsp", None),
                "serial_number": getattr(entry, "serial_number", None),
                "serial_hex": getattr(entry, "serial_hex", None),
                "vendor_id": getattr(entry, "vendor_id", None),
                "model": getattr(entry, "model", None),
                "software_version": getattr(entry, "software_version", None),
                "mac": getattr(entry, "mac", None),
                "equipment_sn": getattr(entry, "equipment_sn", None),
                "autofind_time": getattr(entry, "autofind_time", None),
            }
            for entry in entries
        ],
    }


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
    existing_candidates = list(
        db.scalars(
            select(OltAutofindCandidate).where(
                OltAutofindCandidate.olt_id == olt.id,
            )
        ).all()
    )
    active_entries = [
        candidate for candidate in existing_candidates if candidate.is_active
    ]
    by_key: dict[tuple[str, str], OltAutofindCandidate] = {}
    for item in existing_candidates:
        for serial in _candidate_serial_values(item):
            by_key.setdefault((item.fsp, serial), item)
    seen_keys: set[tuple[str, str]] = set()
    created = 0
    updated = 0
    resolved = 0

    for entry in entry_list:
        serial_number = _entry_serial(entry)
        fsp = str(getattr(entry, "fsp", "") or "").strip()
        if not fsp or not serial_number:
            continue
        serial_hex = str(getattr(entry, "serial_hex", "") or "").strip()
        entry_serials = [
            normalize_serial(candidate)
            for candidate in serial_search_candidates(serial_number)
        ]
        entry_serials.extend(
            normalize_serial(candidate)
            for candidate in serial_search_candidates(serial_hex)
        )
        entry_serials = [
            candidate for candidate in dict.fromkeys(entry_serials) if candidate
        ]
        key = (fsp, entry_serials[0])
        seen_keys.add(key)
        candidate = next(
            (
                by_key.get((fsp, serial))
                for serial in entry_serials
                if by_key.get((fsp, serial))
            ),
            None,
        )
        matched_ont = _find_ont_by_serial(db, serial_number)
        if candidate is None:
            candidate = OltAutofindCandidate(
                olt_id=olt.id,
                ont_unit_id=matched_ont.id if matched_ont else None,
                fsp=fsp,
                serial_number=serial_number,
                serial_hex=serial_hex or None,
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
            candidate.fsp = fsp
            candidate.serial_number = serial_number
            candidate.serial_hex = serial_hex or None
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
        keys = {
            (candidate.fsp, serial)
            for serial in _candidate_serial_values(candidate)
        }
        if keys.intersection(seen_keys):
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
    matched_ont = _find_ont_by_serial(db, candidate.serial_number)
    if matched_ont:
        candidate.ont_unit_id = matched_ont.id
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


def build_unconfigured_onts_redirect_url(
    *,
    search: str | None = None,
    olt_id: str | None = None,
    view: str | None = None,
    resolution: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> str:
    """Build the canonical ONT inventory URL for unconfigured ONT filters."""
    params = ["view=unconfigured"]
    if view:
        params.append(f"candidate_view={quote_plus(view)}")
    if resolution:
        params.append(f"resolution={quote_plus(resolution)}")
    if search:
        params.append(f"search={quote_plus(search)}")
    if olt_id:
        params.append(f"olt_id={quote_plus(olt_id)}")
    if status:
        params.append(f"status={quote_plus(status)}")
    if message:
        params.append(f"message={quote_plus(message)}")
    return f"/admin/network/onts?{'&'.join(params)}"


def build_unconfigured_onts_feedback_url(*, status: str, message: str) -> str:
    """Build an unconfigured ONT redirect with a status message."""
    return build_unconfigured_onts_redirect_url(status=status, message=message)
