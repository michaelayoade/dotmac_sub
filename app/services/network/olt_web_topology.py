"""PON topology helpers for OLT web workflows."""

from __future__ import annotations

import logging
from hashlib import blake2b

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import (
    OltCard,
    OltCardPort,
    OltPortType,
    OltShelf,
    PonPort,
    PonPortSplitterLink,
)
from app.services.network.olt_web_audit import (
    actor_name_from_request,
    log_olt_audit_event,
)

logger = logging.getLogger(__name__)

def _olt_sync_lock_key(olt_id: str) -> int:
    """Return a deterministic positive bigint advisory-lock key for an OLT."""
    digest = blake2b(olt_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False) & 0x7FFFFFFFFFFFFFFF


def _resolve_pon_card_port(
    db: Session,
    *,
    olt_id: object,
    board: str | None,
    port: str | None,
) -> tuple[OltCardPort | None, int | None]:
    """Best-effort resolve canonical card-port linkage from board/port values."""
    board_text = str(board or "").strip()
    port_text = str(port or "").strip()
    if not board_text or not port_text or not port_text.isdigit():
        return None, None

    board_parts = board_text.split("/")
    if len(board_parts) != 2 or not all(part.isdigit() for part in board_parts):
        return None, int(port_text)

    shelf_number = int(board_parts[0])
    slot_number = int(board_parts[1])
    port_number = int(port_text)

    card_stmt = (
        select(OltCard)
        .join(OltShelf, OltCard.shelf_id == OltShelf.id)
        .where(OltShelf.olt_id == olt_id)
        .where(OltShelf.shelf_number == shelf_number)
        .where(OltCard.slot_number == slot_number)
        .limit(1)
    )
    card = db.scalars(card_stmt).first()
    if card is None:
        return None, port_number

    stmt = (
        select(OltCardPort)
        .where(OltCardPort.card_id == card.id)
        .where(OltCardPort.port_number == port_number)
        .limit(1)
    )
    card_port = db.scalars(stmt).first()
    if card_port is None:
        card_port = OltCardPort(
            card_id=card.id,
            port_number=port_number,
            port_type=OltPortType.pon,
            name=f"{board_text}/{port_text}",
            is_active=True,
        )
        db.add(card_port)
        db.flush()
    return card_port, port_number


def _ensure_canonical_pon_port(
    db: Session,
    *,
    olt_id: object,
    fsp: str,
    board: str | None,
    port: str | None,
) -> PonPort:
    """Find or create a PON port row and backfill canonical topology metadata."""
    resolved_card_port, resolved_port_number = _resolve_pon_card_port(
        db,
        olt_id=olt_id,
        board=board,
        port=port,
    )
    name_match = db.scalars(
        select(PonPort).where(
            PonPort.olt_id == olt_id,
            PonPort.name == fsp,
        )
    ).first()
    card_port_match = None
    if resolved_card_port is not None:
        card_port_match = db.scalars(
            select(PonPort).where(
                PonPort.olt_id == olt_id,
                PonPort.olt_card_port_id == resolved_card_port.id,
            )
        ).first()
    pon_port = card_port_match or name_match
    duplicate_port = None
    if (
        card_port_match is not None
        and name_match is not None
        and card_port_match.id != name_match.id
    ):
        duplicate_port = name_match
    if pon_port is None:
        pon_port = PonPort(
            olt_id=olt_id,
            name=fsp,
            olt_card_port_id=(
                resolved_card_port.id if resolved_card_port is not None else None
            ),
            port_number=resolved_port_number,
            is_active=True,
        )
        db.add(pon_port)
        db.flush()
        return pon_port

    if duplicate_port is not None:
        _retire_duplicate_pon_port(db, survivor=pon_port, duplicate=duplicate_port)
    pon_port.is_active = True
    pon_port.name = fsp
    if resolved_port_number is not None and pon_port.port_number != resolved_port_number:
        pon_port.port_number = resolved_port_number
    if (
        resolved_card_port is not None
        and pon_port.olt_card_port_id != resolved_card_port.id
    ):
        pon_port.olt_card_port_id = resolved_card_port.id
    return pon_port


def ensure_canonical_pon_port(
    db: Session,
    *,
    olt_id: object,
    fsp: str,
    board: str | None,
    port: str | None,
) -> PonPort:
    """Public wrapper for canonical PON port reconciliation."""
    return _ensure_canonical_pon_port(
        db,
        olt_id=olt_id,
        fsp=fsp,
        board=board,
        port=port,
    )


def _retire_duplicate_pon_port(
    db: Session,
    *,
    survivor: PonPort,
    duplicate: PonPort,
) -> None:
    """Retire a duplicate PON row after moving live references to the survivor."""
    if survivor.id == duplicate.id:
        return
    for assignment in list(getattr(duplicate, "ont_assignments", []) or []):
        if getattr(assignment, "active", False):
            assignment.pon_port_id = survivor.id
    splitter_link = db.scalars(
        select(PonPortSplitterLink)
        .where(PonPortSplitterLink.pon_port_id == duplicate.id)
        .limit(1)
    ).first()
    if splitter_link is not None:
        existing_splitter_link = db.scalars(
            select(PonPortSplitterLink)
            .where(PonPortSplitterLink.pon_port_id == survivor.id)
            .limit(1)
        ).first()
        if existing_splitter_link is None:
            splitter_link.pon_port_id = survivor.id
        else:
            db.delete(splitter_link)
    duplicate.is_active = False
    duplicate.olt_card_port_id = None
    duplicate.name = f"merged:{duplicate.id}"
    db.flush()


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
        key=lambda assignment: (not bool(getattr(assignment, "active", False))),
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
    """Repair and reconcile corrupted PON port rows for an OLT."""
    from app.services.network.olt_web_forms import get_olt_or_none

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", {"scanned": 0, "repaired": 0, "merged": 0, "unresolved": 0}

    ports = list(
        db.scalars(
            select(PonPort)
            .where(PonPort.olt_id == olt.id)
            .order_by(PonPort.created_at.asc(), PonPort.name.asc())
        ).all()
    )

    repaired = 0
    merged = 0
    unresolved: list[dict[str, str]] = []

    try:
        for port in ports:
            if not getattr(port, "is_active", True):
                continue

            target_fsp, board, pon_port, reason = _infer_pon_repair_target(db, port)
            if not target_fsp or not board or not pon_port:
                unresolved.append(
                    {
                        "pon_port_id": str(port.id),
                        "name": str(getattr(port, "name", "") or ""),
                        "reason": reason or "Unable to infer canonical topology",
                    }
                )
                continue

            before = (
                str(getattr(port, "name", "") or ""),
                getattr(port, "port_number", None),
                str(getattr(port, "olt_card_port_id", "") or ""),
                bool(getattr(port, "is_active", True)),
            )
            survivor = _ensure_canonical_pon_port(
                db,
                olt_id=olt.id,
                fsp=target_fsp,
                board=board,
                port=pon_port,
            )
            if survivor.id != port.id:
                _retire_duplicate_pon_port(db, survivor=survivor, duplicate=port)
                merged += 1
                continue

            after = (
                str(getattr(survivor, "name", "") or ""),
                getattr(survivor, "port_number", None),
                str(getattr(survivor, "olt_card_port_id", "") or ""),
                bool(getattr(survivor, "is_active", True)),
            )
            if after != before:
                repaired += 1

        db.commit()
    except Exception as exc:
        db.rollback()
        return (
            False,
            f"Failed to repair PON ports: {exc!s}",
            {
                "scanned": len(ports),
                "repaired": repaired,
                "merged": merged,
                "unresolved": len(unresolved),
                "unresolved_ports": unresolved,
            },
        )

    message = (
        f"PON port repair complete: scanned {len(ports)}, repaired {repaired}, "
        f"merged {merged}, unresolved {len(unresolved)}."
    )
    return (
        True,
        message,
        {
            "scanned": len(ports),
            "repaired": repaired,
            "merged": merged,
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
    """Tracked wrapper around repair_pon_ports_for_olt."""
    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    initiated_by = initiated_by or actor_name_from_request(request)
    try:
        op = network_operations.start(
            db,
            NetworkOperationType.olt_pon_repair,
            NetworkOperationTargetType.olt,
            olt_id,
            correlation_key=f"olt_pon_repair:{olt_id}",
            initiated_by=initiated_by,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return False, "A PON repair is already in progress for this OLT.", {}
        raise
    network_operations.mark_running(db, str(op.id))
    db.flush()

    try:
        success, message, stats = repair_pon_ports_for_olt(db, olt_id)
        try:
            payload = {"mode": "pon_port_repair", **dict(stats)}
            if success:
                network_operations.mark_succeeded(
                    db, str(op.id), output_payload=payload
                )
            else:
                network_operations.mark_failed(
                    db, str(op.id), message, output_payload=payload
                )
        except Exception as track_err:
            logger.error(
                "Failed to record PON repair outcome for %s: %s", op.id, track_err
            )
        log_olt_audit_event(
            db,
            request=request,
            action="repair_pon_ports",
            entity_id=olt_id,
            metadata={
                "result": "success" if success else "error",
                "message": message,
                "stats": stats,
            },
            status_code=200 if success else 500,
            is_success=success,
        )
        return success, message, stats
    except Exception as exc:
        try:
            network_operations.mark_failed(db, str(op.id), str(exc))
        except Exception as track_err:
            logger.error(
                "Failed to record PON repair failure for %s: %s (original: %s)",
                op.id,
                track_err,
                exc,
            )
            db.rollback()
        raise
