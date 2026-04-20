"""Keep ONT assignments aligned with authoritative OLT scan details."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit, PonPort
from app.services.network.olt_web_topology import ensure_canonical_pon_port


@dataclass(frozen=True)
class AssignmentAlignmentResult:
    assignment: OntAssignment
    pon_port: PonPort
    created: bool = False
    updated: bool = False
    reactivated: bool = False

    @property
    def changed(self) -> bool:
        return self.created or self.updated or self.reactivated


@dataclass(frozen=True)
class OntTopologyAlignmentResult:
    """Result of aligning direct ONT topology to an authoritative F/S/P."""

    pon_port: PonPort
    updated: bool = False


def parse_fsp_parts(fsp: str) -> tuple[str | None, str | None]:
    """Split an F/S/P string into stored board and port fragments."""
    parts = [part.strip() for part in str(fsp or "").split("/") if part.strip()]
    if len(parts) != 3:
        return None, None
    return f"{parts[0]}/{parts[1]}", parts[2]


def align_ont_assignment_to_authoritative_fsp(
    db: Session,
    *,
    ont: OntUnit,
    olt_id: object,
    fsp: str,
    assigned_at: datetime | None = None,
    create_missing_assignment: bool = True,
    reactivate_existing_assignment: bool = True,
) -> AssignmentAlignmentResult | None:
    """Point the ONT's active assignment at the PON from the OLT scan.

    The OLT scan/autofind row is the source of truth for the physical F/S/P.
    Existing subscriber and service-address links are preserved while the PON
    pointer is corrected to the canonical modeled port.
    """
    board, port = parse_fsp_parts(fsp)
    if not board or not port:
        return None

    now = assigned_at or datetime.now(UTC)
    topology = sync_ont_topology_to_authoritative_fsp(
        db,
        ont=ont,
        olt_id=olt_id,
        fsp=fsp,
    )
    if topology is None:
        return None
    pon_port = topology.pon_port

    active_assignment = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(
            OntAssignment.assigned_at.desc(),
            OntAssignment.created_at.desc(),
        )
        .with_for_update()
    ).first()
    if active_assignment is not None:
        updated = active_assignment.pon_port_id != pon_port.id
        if updated:
            active_assignment.pon_port_id = pon_port.id
        if active_assignment.assigned_at is None:
            active_assignment.assigned_at = now
            updated = True
        return AssignmentAlignmentResult(
            assignment=active_assignment,
            pon_port=pon_port,
            updated=updated,
        )

    latest_assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .order_by(
            OntAssignment.created_at.desc(),
            OntAssignment.assigned_at.desc(),
        )
        .with_for_update()
    ).first()
    if latest_assignment is not None and reactivate_existing_assignment:
        latest_assignment.pon_port_id = pon_port.id
        latest_assignment.active = True
        if latest_assignment.assigned_at is None:
            latest_assignment.assigned_at = now
        return AssignmentAlignmentResult(
            assignment=latest_assignment,
            pon_port=pon_port,
            reactivated=True,
        )

    if not create_missing_assignment:
        return None

    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        active=True,
        assigned_at=now,
    )
    db.add(assignment)
    return AssignmentAlignmentResult(
        assignment=assignment,
        pon_port=pon_port,
        created=True,
    )


def sync_ont_topology_to_authoritative_fsp(
    db: Session,
    *,
    ont: OntUnit,
    olt_id: object,
    fsp: str,
) -> OntTopologyAlignmentResult | None:
    """Align direct ONT topology without implying customer assignment."""
    board, port = parse_fsp_parts(fsp)
    if not board or not port:
        return None

    pon_port = ensure_canonical_pon_port(
        db,
        olt_id=olt_id,
        fsp=fsp,
        board=board,
        port=port,
    )
    updated = False
    if getattr(ont, "olt_device_id", None) is None:
        ont.olt_device_id = pon_port.olt_id
        updated = True
    return OntTopologyAlignmentResult(
        pon_port=pon_port,
        updated=updated,
    )
