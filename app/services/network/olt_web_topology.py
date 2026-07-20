"""PON topology helpers for OLT web workflows."""

from __future__ import annotations

from hashlib import blake2b

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import (
    OltCard,
    OltCardPort,
    OltShelf,
    PonPort,
)
from app.services.network.olt_web_audit import (
    log_olt_audit_event,
)


def _olt_sync_lock_key(olt_id: str) -> int:
    """Return a deterministic positive bigint advisory-lock key for an OLT."""
    digest = blake2b(olt_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False) & 0x7FFFFFFFFFFFFFFF


def _card_port_fsp(db: Session, card_port: OltCardPort) -> str | None:
    """Return canonical shelf/slot/port text for a linked card port."""
    card = db.get(OltCard, card_port.card_id)
    shelf = db.get(OltShelf, card.shelf_id) if card else None
    if shelf is None or card is None:
        return None
    return f"{shelf.shelf_number}/{card.slot_number}/{card_port.port_number}"


def _parse_fsp_parts(fsp: str) -> tuple[str | None, str | None]:
    parts = [part for part in str(fsp or "").split("/") if part != ""]
    if len(parts) < 3:
        return None, None
    return f"{parts[-3]}/{parts[-2]}", parts[-1]


def _infer_pon_repair_target(
    db: Session,
    port: PonPort,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Infer canonical repair target for a possibly-corrupted PON port."""
    if getattr(port, "olt_card_port_id", None):
        card_port = db.get(OltCardPort, port.olt_card_port_id)
        if card_port is not None:
            fsp = _card_port_fsp(db, card_port)
            if fsp:
                board, pon_port = _parse_fsp_parts(fsp)
                return fsp, board, pon_port, None

    parsed_board, parsed_port = _parse_fsp_parts(getattr(port, "name", None) or "")
    if parsed_board and parsed_port:
        return f"{parsed_board}/{parsed_port}", parsed_board, parsed_port, None

    assignments = sorted(
        getattr(port, "ont_assignments", []) or [],
        key=lambda assignment: not bool(getattr(assignment, "active", False)),
    )
    for assignment in assignments:
        ont = getattr(assignment, "ont_unit", None)
        board = str(getattr(ont, "board", "") or "").strip()
        pon_port = str(getattr(ont, "port", "") or "").strip()
        if board and pon_port:
            return f"{board}/{pon_port}", board, pon_port, None

    return None, None, None, "Unable to infer canonical topology"


def repair_pon_ports_for_olt(
    db: Session,
    olt_id: str,
) -> tuple[bool, str, dict[str, object]]:
    """Report inferred PON repair candidates without mutating canonical rows."""
    from app.services.network.olt_web_forms import get_olt_or_none

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return (
            False,
            "OLT not found",
            {"scanned": 0, "repaired": 0, "merged": 0, "unresolved": 0},
        )

    ports = list(
        db.scalars(
            select(PonPort)
            .where(PonPort.olt_id == olt.id)
            .order_by(PonPort.created_at.asc(), PonPort.name.asc())
        ).all()
    )

    unresolved: list[dict[str, str]] = []
    for port in ports:
        if not getattr(port, "is_active", True):
            continue
        target_fsp, _board, pon_port, reason = _infer_pon_repair_target(db, port)
        current_name = str(getattr(port, "name", "") or "")
        current_number = getattr(port, "port_number", None)
        target_number = int(pon_port) if pon_port and pon_port.isdigit() else None
        if not target_fsp or target_number is None:
            candidate_reason = reason or "Unable to infer canonical topology"
        elif current_name == target_fsp and current_number == target_number:
            continue
        else:
            candidate_reason = (
                "inferred PON mutation requires reviewed electronic-topology repair"
            )
        unresolved.append(
            {
                "pon_port_id": str(port.id),
                "name": current_name,
                "reason": candidate_reason,
                "target_fsp": target_fsp or "",
            }
        )

    success = not unresolved
    message = (
        "No inferred PON repair candidates found."
        if success
        else "Direct inferred PON repair is retired; "
        f"{len(unresolved)} candidate(s) require reviewed resolution."
    )
    return (
        success,
        message,
        {
            "scanned": len(ports),
            "repaired": 0,
            "merged": 0,
            "unresolved": len(unresolved),
            "unresolved_ports": unresolved,
        },
    )


def repair_pon_ports_for_olt_tracked(
    db: Session,
    olt_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> tuple[bool, str, dict[str, object]]:
    """Compatibility adapter for the retired repair action; audit only."""
    success, message, stats = repair_pon_ports_for_olt(db, olt_id)
    log_olt_audit_event(
        db,
        request=request,
        action="audit_pon_port_repair_candidates",
        entity_id=olt_id,
        metadata={
            "actor": initiated_by,
            "candidate_free": success,
            "message": message,
            "stats": stats,
        },
        status_code=200,
        is_success=True,
    )
    return success, message, stats
