"""Web service for OLT service-port management on ONTs."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.olt_protocol_adapters import get_protocol_adapter
from app.services.network.olt_ssh import ServicePortEntry, create_service_ports
from app.services.network.olt_ssh_ont import ServicePortDiagnostics
from app.services.network.ont_olt_context import resolve_ont_olt_write_context

if TYPE_CHECKING:
    pass  # ServicePortEntry already imported above
from app.services.network.service_port_allocator import (
    AllocationError,
    allocate_service_port,
    find_allocation_by_index,
    mark_provisioned,
    release_service_port,
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
    ctx, _message = resolve_ont_olt_write_context(db, ont_id)
    if ctx is not None:
        return ctx.ont, ctx.olt, ctx.fsp, ctx.ont_id_on_olt

    ont = db.get(OntUnit, ont_id)
    return ont, None, None, None


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
    adapter = get_protocol_adapter(olt)
    result = adapter.get_service_ports_for_ont(fsp, olt_ont_id)
    if not result.success:
        context["error"] = result.message
        return context

    ports_data = result.data.get("service_ports", [])
    ports: list[ServicePortEntry] = ports_data if isinstance(ports_data, list) else []
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


def coerce_user_vlan(value: str) -> tuple[int | str | None, str | None]:
    """Normalize service-port user VLAN form input."""
    raw_user_vlan = value.strip()
    if not raw_user_vlan:
        return None, None
    if raw_user_vlan == "untagged":
        return "untagged", None
    try:
        return int(raw_user_vlan), None
    except ValueError:
        return None, "User VLAN must be a number or 'untagged'"


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

    Uses the DB-based service port allocator to pre-allocate an index,
    then creates the service-port on the OLT with that index.

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

    # Pre-allocate service-port index from DB
    allocation = None
    try:
        allocation = allocate_service_port(
            db,
            olt.id,
            ont_id,
            vlan_id=vlan_id,
            gem_index=gem_index,
            service_type="internet" if vlan_id in (203,) else "management",
        )
        port_index = allocation.port_index
        logger.info(
            "Pre-allocated service-port index %d for ONT %s VLAN %d",
            port_index,
            ont_id,
            vlan_id,
        )
    except AllocationError as exc:
        logger.error("Failed to allocate service-port index: %s", exc)
        return False, f"Allocation failed: {exc}"

    # Create on OLT with pre-allocated index
    adapter = get_protocol_adapter(olt)
    result = adapter.create_service_port(
        fsp,
        olt_ont_id,
        gem_index=gem_index,
        vlan_id=vlan_id,
        user_vlan=user_vlan,
        tag_transform=tag_transform,
        port_index=port_index,
    )
    ok = result.success
    msg = result.message

    if ok:
        # Mark allocation as provisioned
        mark_provisioned(db, allocation.id)
        db.commit()
        return True, f"Service-port {port_index} created (VLAN {vlan_id}, GEM {gem_index})"
    else:
        # OLT creation failed - release the allocation
        release_service_port(db, allocation.id)
        db.commit()
        return False, msg


def handle_delete(
    db: Session,
    ont_id: str,
    index: int,
) -> tuple[bool, str]:
    """Delete a service-port from the OLT by index.

    Also releases the corresponding DB allocation if one exists.

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

    # Delete from OLT
    adapter = get_protocol_adapter(olt)
    result = adapter.delete_service_port(index)
    ok = result.success
    msg = result.message

    if ok:
        # Release DB allocation if exists
        allocation = find_allocation_by_index(db, str(olt.id), index)
        if allocation:
            release_service_port(db, str(allocation.id))
            db.commit()
            logger.info(
                "Released service-port allocation %d for ONT %s",
                index,
                ont_id,
            )

    return ok, msg


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
    adapter = get_protocol_adapter(ref_olt)
    result = adapter.get_service_ports_for_ont(ref_fsp, ref_olt_ont_id)
    if not result.success:
        return False, f"Could not get reference ports: {result.message}"

    ref_ports_data = result.data.get("service_ports", [])
    ref_ports: list[ServicePortEntry] = (
        ref_ports_data if isinstance(ref_ports_data, list) else []
    )
    if not ref_ports:
        return False, f"Could not get reference ports: {result.message}"

    # Create on target using existing bulk function
    return create_service_ports(olt, fsp, olt_ont_id, ref_ports)


def handle_diagnose(
    db: Session,
    ont_id: str,
) -> tuple[bool, str, ServicePortDiagnostics | None]:
    """Run diagnostics to troubleshoot service port state issues.

    Args:
        db: Database session.
        ont_id: OntUnit ID.

    Returns:
        (success, message, diagnostics).
    """
    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT", None

    adapter = get_protocol_adapter(olt)
    result = adapter.diagnose_service_ports(fsp, olt_ont_id)
    diagnostics_data = result.data.get("diagnostics")
    # Cast to expected type - adapter returns ServicePortDiagnostics or None
    diagnostics: ServicePortDiagnostics | None = (
        diagnostics_data if isinstance(diagnostics_data, ServicePortDiagnostics) else None
    )
    return result.success, result.message, diagnostics
