"""Project non-conflicting OLT observations into initially empty ONT topology."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit, PonPort
from app.services.network.ont_topology_observations import (
    observe_ont_electronic_topology,
)


@dataclass(frozen=True)
class AssignmentAlignmentResult:
    assignment: OntAssignment
    pon_port: PonPort
    review_required: bool = False
    review_reason: str | None = None


@dataclass(frozen=True)
class OntTopologyAlignmentResult:
    """Result of projecting an observation into direct ONT topology."""

    pon_port: PonPort | None
    updated: bool = False
    review_required: bool = False
    review_reason: str | None = None


def parse_fsp_parts(fsp: str) -> tuple[str | None, str | None]:
    """Split an F/S/P string into stored board and port fragments."""
    parts = [part.strip() for part in str(fsp or "").split("/") if part.strip()]
    if len(parts) != 3:
        return None, None
    return f"{parts[0]}/{parts[1]}", parts[2]


def check_ont_assignment_against_fsp_observation(
    db: Session,
    *,
    ont: OntUnit,
    olt_id: str | uuid.UUID,
    fsp: str,
) -> AssignmentAlignmentResult | None:
    """Check an active assignment against a non-conflicting OLT observation.

    This compatibility adapter no longer creates, reactivates, or rewrites an
    assignment. OLT scans are observations; conflicting assignment identity
    must be repaired through ``network.ont_assignment_identity``.
    """
    board, port = parse_fsp_parts(fsp)
    if not board or not port:
        return None

    topology = project_ont_topology_from_fsp_observation(
        db,
        ont=ont,
        olt_id=olt_id,
        fsp=fsp,
    )
    if topology is None or topology.pon_port is None:
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
        assignment_conflict = active_assignment.pon_port_id != pon_port.id
        return AssignmentAlignmentResult(
            assignment=active_assignment,
            pon_port=pon_port,
            review_required=topology.review_required or assignment_conflict,
            review_reason=(
                topology.review_reason
                or (
                    "active assignment PON conflicts with the OLT observation"
                    if assignment_conflict
                    else None
                )
            ),
        )
    return None


def project_ont_topology_from_fsp_observation(
    db: Session,
    *,
    ont: OntUnit,
    olt_id: str | uuid.UUID,
    fsp: str,
) -> OntTopologyAlignmentResult | None:
    """Delegate an exact Huawei F/S/P observation to the canonical owner."""
    board, port = parse_fsp_parts(fsp)
    if not board or not port:
        return None
    result = observe_ont_electronic_topology(
        db,
        source="huawei_fsp",
        evidence_key=f"{ont.id}:{olt_id}:{fsp}",
        ont_unit_id=ont.id,
        observed_olt_id=olt_id,
        observed_port_number=int(port) if port.isdigit() else None,
        observed_port_label=fsp,
    )
    return OntTopologyAlignmentResult(
        pon_port=result.pon_port,
        updated=result.ont_updated,
        review_required=result.review_required,
        review_reason=result.reason,
    )
