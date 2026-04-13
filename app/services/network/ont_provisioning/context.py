"""ONT provisioning context resolution."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.serial_utils import parse_ont_id_on_olt


@dataclass
class OltContext:
    """Resolved ONT-to-OLT mapping needed for SSH operations."""

    ont: OntUnit
    olt: OLTDevice
    fsp: str
    olt_ont_id: int
    assignment: OntAssignment | None = None


def resolve_olt_context(db: Session, ont_id: str) -> tuple[OltContext | None, str]:
    """Resolve ONT -> OLT + F/S/P + ONT-ID for SSH operations."""
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return None, "ONT not found"

    assignment: OntAssignment | None = None
    for item in getattr(ont, "assignments", []):
        if item.active:
            assignment = item
            break
    if not assignment:
        return None, "ONT has no active assignment"
    if not assignment.pon_port_id:
        return None, "Assignment has no PON port"

    pon_port: PonPort | None = db.get(PonPort, str(assignment.pon_port_id))
    if not pon_port:
        return None, "PON port not found"

    olt: OLTDevice | None = db.get(OLTDevice, str(pon_port.olt_id))
    if not olt:
        return None, "OLT not found"

    board = ont.board or ""
    port = ont.port or ""
    if not board or not port:
        return (
            None,
            "ONT is missing scanned board/port. Run an OLT scan before provisioning.",
        )
    fsp = f"{board}/{port}"

    olt_ont_id = parse_ont_id_on_olt(ont.external_id)
    if olt_ont_id is None:
        return None, f"No usable ONT-ID in external_id ({ont.external_id!r})"

    return OltContext(ont=ont, olt=olt, fsp=fsp, olt_ont_id=olt_ont_id, assignment=assignment), ""
