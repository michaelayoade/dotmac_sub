"""Web service for OLT service-port management on ONTs."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.olt_ssh import create_service_ports
from app.services.network.olt_ssh_service_ports import (
    create_single_service_port,
    delete_service_port,
    get_service_ports_for_ont,
)
from app.services.network.vlan_chain import validate_chain

logger = logging.getLogger(__name__)


def _normalize_fsp(value: str | None) -> str | None:
    """Normalize stored PON labels to raw frame/slot/port strings."""
    raw = (value or "").strip()
    if raw.lower().startswith("pon-"):
        raw = raw[4:].strip()
    return raw or None


def _parse_ont_id_on_olt(external_id: str | None) -> int | None:
    """Extract the ONT ID from supported external_id formats.

    Supported formats:
    - "5" -> 5
    - "generic:5" -> 5
    - "huawei:4194320640.5" -> 5
    """
    ext = (external_id or "").strip()
    if ext.isdigit():
        return int(ext)
    match = re.match(r"^(?:[a-z0-9_-]+:)?(?:\d+\.)*(\d+)$", ext, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    return None


def _resolve_ont_olt_context(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, OLTDevice | None, str | None, int | None]:
    """Resolve ONT → active assignment → OLT + FSP + ont_id.

    Returns:
        (ont, olt, fsp, ont_id_on_olt) or (None, None, None, None) on failure.
    """
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return None, None, None, None

    # Find active assignment
    assignment: OntAssignment | None = None
    for a in getattr(ont, "assignments", []):
        if a.active:
            assignment = a
            break
    if not assignment:
        return ont, None, None, None

    # Resolve OLT via pon_port
    pon_port: PonPort | None = db.get(PonPort, str(assignment.pon_port_id))
    if not pon_port:
        return ont, None, None, None

    olt: OLTDevice | None = db.get(OLTDevice, str(pon_port.olt_id))
    if not olt:
        return ont, None, None, None

    # Build FSP from OntUnit fields (board = "0/2", port = "1" → "0/2/1")
    board = ont.board or ""
    port = ont.port or ""
    if board and port:
        fsp = _normalize_fsp(f"{board}/{port}")
    elif pon_port.name:
        fsp = _normalize_fsp(pon_port.name)
    else:
        return ont, olt, None, None

    # ONT-ID extraction from external_id
    # Formats: "5" (plain), "huawei:4194320640.5" (SNMP), "external:SERIAL" (no ID)
    ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)

    return ont, olt, fsp, ont_id_on_olt


def _reference_ont_options(
    db: Session,
    *,
    target_ont_id: str,
    olt_id: str,
) -> list[dict[str, str]]:
    """Return selectable reference ONTs on the same OLT."""
    assignments = db.scalars(
        select(OntAssignment).where(
            OntAssignment.active.is_(True),
            OntAssignment.pon_port_id.is_not(None),
            OntAssignment.ont_unit_id != target_ont_id,
        )
    ).all()

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for assignment in assignments:
        ont = db.get(OntUnit, str(assignment.ont_unit_id))
        if not ont:
            continue

        pon_port = db.get(PonPort, str(assignment.pon_port_id))
        if not pon_port or str(pon_port.olt_id) != olt_id:
            continue

        ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)
        if ont_id_on_olt is None:
            continue

        option_id = str(ont.id)
        if option_id in seen:
            continue
        seen.add(option_id)

        port_label = pon_port.name or f"{ont.board or '?'} / {ont.port or '?'}"
        serial = ont.serial_number or "Unknown serial"
        label = f"{serial} | ONT-ID {ont_id_on_olt} | {port_label}"
        options.append({"id": option_id, "label": label})

    options.sort(key=lambda item: item["label"].lower())
    return options


def list_context(db: Session, ont_id: str) -> dict[str, Any]:
    """Build context for service-ports tab on ONT detail page.

    Args:
        db: Database session.
        ont_id: OntUnit ID.

    Returns:
        Dict with service_ports, olt, fsp, vlan_chain, errors.
    """
    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)

    context: dict[str, Any] = {
        "ont": ont,
        "olt": olt,
        "fsp": fsp,
        "olt_ont_id": olt_ont_id,
        "service_ports": [],
        "vlan_chain": None,
        "reference_onts": [],
        "error": None,
    }

    if not ont:
        context["error"] = "ONT not found"
        return context

    if not olt or not fsp:
        context["error"] = "ONT has no active assignment with OLT port mapping"
        return context

    if olt_ont_id is None:
        context["error"] = "ONT external ID not set — cannot query service-ports"
        return context

    # Query OLT for service-ports on this ONT
    ok, msg, ports = get_service_ports_for_ont(olt, fsp, olt_ont_id)
    if not ok:
        context["error"] = msg
        return context

    context["service_ports"] = ports
    context["reference_onts"] = _reference_ont_options(
        db,
        target_ont_id=ont_id,
        olt_id=str(olt.id),
    )

    # Run VLAN chain validation
    port_dicts = [{"vlan_id": p.vlan_id} for p in ports]
    chain_result = validate_chain(db, ont_id, actual_service_ports=port_dicts)
    context["vlan_chain"] = chain_result

    # Limit selectable VLANs to the assigned OLT.
    from app.services import web_network_onts as web_network_onts_service

    context["vlans"] = web_network_onts_service.get_vlans_for_olt(
        db,
        str(olt.id),
    )

    return context


def handle_create(
    db: Session,
    ont_id: str,
    vlan_id: int,
    gem_index: int,
    *,
    user_vlan: int | str | None = None,
    tag_transform: str = "translate",
) -> tuple[bool, str]:
    """Create a single service-port on the OLT for this ONT.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        vlan_id: VLAN ID for the service-port.
        gem_index: GEM port index.

    Returns:
        (success, message).
    """
    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"

    allowed_transforms = {"translate", "transparent", "default"}
    if tag_transform not in allowed_transforms:
        return False, "Invalid tag-transform value"

    return create_single_service_port(
        olt,
        fsp,
        olt_ont_id,
        gem_index,
        vlan_id,
        user_vlan=user_vlan,
        tag_transform=tag_transform,
    )


def handle_delete(
    db: Session,
    ont_id: str,
    index: int,
) -> tuple[bool, str]:
    """Delete a service-port from the OLT by index.

    Args:
        db: Database session.
        ont_id: OntUnit ID (for context resolution).
        index: Service-port index on the OLT.

    Returns:
        (success, message).
    """
    ont, olt, fsp, _olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt:
        return False, "Cannot resolve OLT for this ONT"

    return delete_service_port(olt, index)


def handle_clone(
    db: Session,
    ont_id: str,
    ref_ont_id: str,
) -> tuple[bool, str]:
    """Clone service-ports from a reference ONT to this ONT.

    Args:
        db: Database session.
        ont_id: Target OntUnit ID.
        ref_ont_id: Reference OntUnit ID to copy ports from.

    Returns:
        (success, message).
    """
    # Resolve target ONT
    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for target ONT"

    # Resolve reference ONT
    ref_ont, ref_olt, ref_fsp, ref_olt_ont_id = _resolve_ont_olt_context(db, ref_ont_id)
    if not ref_olt or not ref_fsp or ref_olt_ont_id is None:
        return False, "Cannot resolve OLT context for reference ONT"

    # Both ONTs must be on the same OLT
    if str(olt.id) != str(ref_olt.id):
        return False, "Target and reference ONTs must be on the same OLT"

    # Get reference service-ports
    ok, msg, ref_ports = get_service_ports_for_ont(ref_olt, ref_fsp, ref_olt_ont_id)
    if not ok or not ref_ports:
        return False, f"Could not get reference ports: {msg}"

    # Create on target using existing bulk function
    return create_service_ports(olt, fsp, olt_ont_id, ref_ports)
