"""Strict ONT-to-OLT context resolution for write operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.serial_utils import parse_ont_id_on_olt


@dataclass(frozen=True)
class OntOltWriteContext:
    """Complete OLT target context required before mutating an ONT on an OLT."""

    ont: OntUnit
    olt: OLTDevice
    assignment: OntAssignment
    pon_port: PonPort
    fsp: str
    ont_id_on_olt: int


@dataclass(frozen=True)
class OntOltReadContext:
    """Committed OLT target context for read-only ONT page projections."""

    ont: OntUnit
    olt: OLTDevice
    assignment: OntAssignment
    pon_port: PonPort
    fsp: str
    ont_id_on_olt: int


_FSP_RE = re.compile(r"^\d+/\d+/\d+$")


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


def _scanned_fsp_from_ont(ont: OntUnit) -> str | None:
    board = str(getattr(ont, "board", "") or "").strip()
    port = str(getattr(ont, "port", "") or "").strip()
    if not board or not port:
        return None
    fsp = f"{board}/{port}"
    return fsp if _FSP_RE.fullmatch(fsp) else None


def _active_assignment_statement(
    ont_id: object, *, for_update: bool
) -> Select[tuple[OntAssignment]]:
    stmt = (
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
        .order_by(
            OntAssignment.assigned_at.desc(),
            OntAssignment.created_at.desc(),
        )
    )
    return stmt.with_for_update() if for_update else stmt


def _load_active_assignment(
    db: Session,
    *,
    ont_id: object,
) -> OntAssignment | None:
    """Load the active assignment used for strict OLT write context resolution.

    Lock the selected active assignment so concurrent inventory or sync jobs
    cannot change the write target while an OLT mutation is being prepared.
    """
    stmt = _active_assignment_statement(ont_id, for_update=True)
    return db.scalars(stmt).first()


def _load_active_assignment_for_read(
    db: Session,
    *,
    ont_id: object,
) -> OntAssignment | None:
    """Load the active assignment for display without taking write locks."""
    stmt = _active_assignment_statement(ont_id, for_update=False)
    return db.scalars(stmt).first()


def resolve_ont_olt_write_context(
    db: Session,
    ont_id: str,
) -> tuple[OntOltWriteContext | None, str | None]:
    """Resolve the exact OLT/FSP/ONT-ID context required for OLT writes.

    This deliberately refuses display/name fallbacks. Writes must target the
    OLT-scanned board/port and ONT-ID recorded for the ONT.
    """
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return None, "ONT not found."

    assignment = _load_active_assignment(db, ont_id=ont.id)
    if assignment is None:
        return None, "ONT has no active assignment."
    if not assignment.pon_port_id:
        return None, "ONT active assignment has no PON port."

    pon_port = db.get(PonPort, str(assignment.pon_port_id))
    if pon_port is None:
        return None, "Assigned PON port not found."

    olt = db.get(OLTDevice, str(pon_port.olt_id))
    if olt is None:
        return None, "Assigned PON port is not linked to an OLT."

    fsp = _scanned_fsp_from_ont(ont)
    if fsp is None:
        return (
            None,
            "ONT is missing scanned board/port. Run an OLT scan before this action.",
        )

    ont_id_on_olt = parse_ont_id_on_olt(getattr(ont, "external_id", None))
    if ont_id_on_olt is None:
        return None, "ONT external_id does not contain a usable ONT-ID."

    return (
        OntOltWriteContext(
            ont=ont,
            olt=olt,
            assignment=assignment,
            pon_port=pon_port,
            fsp=fsp,
            ont_id_on_olt=ont_id_on_olt,
        ),
        None,
    )


def resolve_ont_olt_read_context(
    db: Session,
    ont_id: str,
) -> tuple[OntOltReadContext | None, str | None]:
    """Resolve OLT/FSP/ONT-ID context for read-only ONT page projections."""
    ont_uuid = _uuid_or_none(ont_id)
    if ont_uuid is None:
        return None, "ONT id is not a valid UUID."

    ont = db.get(OntUnit, ont_uuid)
    if ont is None:
        return None, "ONT not found."

    assignment = _load_active_assignment_for_read(db, ont_id=ont.id)
    if assignment is None:
        return None, "ONT has no active assignment."
    if not assignment.pon_port_id:
        return None, "ONT active assignment has no PON port."

    pon_port = db.get(PonPort, assignment.pon_port_id)
    if pon_port is None:
        return None, "Assigned PON port not found."

    olt = db.get(OLTDevice, pon_port.olt_id)
    if olt is None:
        return None, "Assigned PON port is not linked to an OLT."

    fsp = _scanned_fsp_from_ont(ont)
    if fsp is None:
        return (
            None,
            "ONT is missing scanned board/port. Run an OLT scan before this action.",
        )

    ont_id_on_olt = parse_ont_id_on_olt(getattr(ont, "external_id", None))
    if ont_id_on_olt is None:
        return None, "ONT external_id does not contain a usable ONT-ID."

    return (
        OntOltReadContext(
            ont=ont,
            olt=olt,
            assignment=assignment,
            pon_port=pon_port,
            fsp=fsp,
            ont_id_on_olt=ont_id_on_olt,
        ),
        None,
    )
