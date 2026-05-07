"""Web service for OLT profile display (line, service, TR-069, WAN profiles)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import (
    OLTDevice,
    OltLineProfileGemMapping,
    OltLineProfile,
    OltOnuTypeProfileMapping,
    OltServiceProfile,
    OltServicePort,
    OntProvisioningProfile,
)
from app.services.network.imported_service_ports import imported_service_port_summary
from app.services.network import olt as olt_service
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OltCommandSet,
    OntProvisioningContext,
    build_spec_from_profile,
)
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_provisioning.credentials import mask_credentials
from app.services.olt_profile_adapter import olt_profile_adapter
from app.services.web_network_service_ports import _resolve_ont_olt_context

logger = logging.getLogger(__name__)


def line_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT line and service profiles through the profile adapter."""
    return olt_profile_adapter.line_profiles_context(db, olt_id)


def tr069_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT TR-069 server profiles through the profile adapter."""
    return olt_profile_adapter.tr069_profiles_context(db, olt_id)


def imported_profile_state_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Return imported OLT profile state from DB source-of-truth tables."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return {
            "olt": None,
            "line_profiles": [],
            "service_profiles": [],
            "profile_mappings": [],
            "error": "OLT not found",
        }

    line_profiles = list(
        db.scalars(
            select(OltLineProfile)
            .where(OltLineProfile.olt_id == olt.id)
            .order_by(OltLineProfile.profile_id)
        )
    )
    service_profiles = list(
        db.scalars(
            select(OltServiceProfile)
            .where(OltServiceProfile.olt_id == olt.id)
            .order_by(OltServiceProfile.profile_id)
        )
    )
    profile_mappings = list(
        db.scalars(
            select(OltOnuTypeProfileMapping)
            .where(OltOnuTypeProfileMapping.olt_id == olt.id)
            .order_by(OltOnuTypeProfileMapping.equipment_id)
        )
    )
    gem_mappings = list(
        db.scalars(
            select(OltLineProfileGemMapping)
            .where(OltLineProfileGemMapping.olt_id == olt.id)
            .order_by(
                OltLineProfileGemMapping.line_profile_id,
                OltLineProfileGemMapping.source,
                OltLineProfileGemMapping.vlan_id,
                OltLineProfileGemMapping.gem_index,
            )
        )
    )
    service_ports = list(
        db.scalars(
            select(OltServicePort)
            .where(OltServicePort.olt_device_id == olt.id)
            .order_by(OltServicePort.port_index)
            .limit(100)
        )
    )
    service_port_summary = imported_service_port_summary(db, olt_id=olt.id)
    return {
        "olt": olt,
        "line_profiles": line_profiles,
        "service_profiles": service_profiles,
        "profile_mappings": profile_mappings,
        "gem_mappings": gem_mappings,
        "service_ports": service_ports,
        "service_port_summary": service_port_summary,
        "error": None,
    }


def save_imported_profile_mapping(
    db: Session,
    olt_id: str,
    *,
    equipment_id: str,
    line_profile_id: int,
    service_profile_id: int,
) -> tuple[bool, str]:
    """Create or update an OLT equipment mapping using imported profiles only."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found"

    clean_equipment_id = equipment_id.strip()
    if not clean_equipment_id:
        return False, "Equipment ID is required"

    line_profile = db.scalars(
        select(OltLineProfile)
        .where(OltLineProfile.olt_id == olt.id)
        .where(OltLineProfile.profile_id == line_profile_id)
    ).first()
    if line_profile is None:
        return (
            False,
            f"Line profile {line_profile_id} has not been imported for {olt.name}",
        )

    service_profile = db.scalars(
        select(OltServiceProfile)
        .where(OltServiceProfile.olt_id == olt.id)
        .where(OltServiceProfile.profile_id == service_profile_id)
    ).first()
    if service_profile is None:
        return (
            False,
            f"Service profile {service_profile_id} has not been imported for {olt.name}",
        )

    mapping = db.scalars(
        select(OltOnuTypeProfileMapping)
        .where(OltOnuTypeProfileMapping.olt_id == olt.id)
        .where(OltOnuTypeProfileMapping.equipment_id == clean_equipment_id)
    ).first()
    created = mapping is None
    if mapping is None:
        mapping = OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id=clean_equipment_id,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
            source_registration_count=0,
        )
        db.add(mapping)
    else:
        mapping.line_profile_id = line_profile_id
        mapping.service_profile_id = service_profile_id

    db.flush()
    action = "Created" if created else "Updated"
    return (
        True,
        (
            f"{action} mapping for {clean_equipment_id}: "
            f"line {line_profile_id}, service {service_profile_id}"
        ),
    )


def delete_imported_profile_mapping(
    db: Session,
    olt_id: str,
    mapping_id: str,
) -> tuple[bool, str]:
    """Delete an explicit imported equipment mapping."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found"

    mapping = db.get(OltOnuTypeProfileMapping, mapping_id)
    if mapping is None or str(mapping.olt_id) != str(olt.id):
        return False, "Mapping not found"

    equipment_id = mapping.equipment_id
    db.delete(mapping)
    db.flush()
    return True, f"Deleted mapping for {equipment_id}"


def propagate_acs_to_onts(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        stats = olt_service.OLTDevices.propagate_acs_to_onts(db, olt_id)
    except HTTPException as exc:
        return exc.status_code, {"ok": False, "message": exc.detail}

    log_olt_audit_event(
        db,
        request=request,
        action="propagate_acs",
        entity_id=olt_id,
        metadata=dict(stats),
    )
    updated = stats["updated"]
    total = stats["total"]
    already = stats["already_bound"]
    if updated:
        message = (
            f"ACS binding propagated to {updated} ONTs "
            f"({already} already bound, {total} total)."
        )
    else:
        message = f"All {total} ONTs already bound to this ACS server."
    return 200, {"ok": True, "message": message, **stats}


def enforce_provisioning(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    from app.services.network.provisioning_enforcement import ProvisioningEnforcement

    stats = ProvisioningEnforcement.run_full_enforcement(db, olt_id=olt_id)
    log_olt_audit_event(
        db,
        request=request,
        action="enforce_provisioning",
        entity_id=olt_id,
        metadata=dict(stats),
    )

    gaps = stats.get("gaps_detected", {})
    total_gaps = sum(gaps.values()) if isinstance(gaps, dict) else 0
    if total_gaps == 0:
        message = "No provisioning gaps detected on this OLT."
    else:
        message = f"Provisioning gap scan complete: {total_gaps} gap(s) detected."
    return 200, {"ok": True, "message": message, **stats}


def backfill_pon_ports(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        stats = olt_service.OLTDevices.backfill_pon_ports(db, olt_id)
    except HTTPException as exc:
        return exc.status_code, {"ok": False, "message": exc.detail}

    log_olt_audit_event(
        db,
        request=request,
        action="backfill_pon_ports",
        entity_id=olt_id,
        metadata=dict(stats),
    )

    created = stats["ports_created"]
    linked = stats["assignments_linked"]
    total = stats["total_onts"]
    parts = []
    if created:
        parts.append(f"{created} PON ports created")
    if linked:
        parts.append(f"{linked} assignments linked")
    if not parts:
        message = f"All PON ports already exist for {total} ONTs."
    else:
        message = f"{', '.join(parts)} ({total} ONTs on this OLT)."
    return 200, {"ok": True, "message": message, **stats}


def command_preview_context(
    db: Session,
    ont_id: str,
    profile_id: str,
    *,
    tr069_olt_profile_id: int | None = None,
) -> dict[str, Any]:
    """Generate provisioning command preview for an ONT + profile combo.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        profile_id: OntProvisioningProfile ID.
        tr069_olt_profile_id: OLT-level TR-069 server profile ID.

    Returns:
        Context dict with command_sets, spec, error.
    """
    context: dict[str, Any] = {
        "command_sets": [],
        "error": None,
        "ont": None,
        "profile": None,
    }

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not ont:
        context["error"] = "ONT not found"
        return context
    context["ont"] = ont

    if not olt or not fsp or olt_ont_id is None:
        context["error"] = (
            "Cannot resolve OLT context — check assignment and external ID"
        )
        return context

    profile = db.get(OntProvisioningProfile, profile_id)
    if not profile:
        context["error"] = "Provisioning profile not found"
        return context
    context["profile"] = profile

    # Build provisioning context
    parts = fsp.split("/")
    prov_context = OntProvisioningContext(
        frame=int(parts[0]) if len(parts) > 0 else 0,
        slot=int(parts[1]) if len(parts) > 1 else 0,
        port=int(parts[2]) if len(parts) > 2 else 0,
        ont_id=olt_ont_id,
        olt_name=olt.name,
    )

    # Get subscriber info if available

    for a in getattr(ont, "assignments", []):
        if a.active and a.subscriber_id:
            from app.models.subscriber import Subscriber

            sub = db.get(Subscriber, str(a.subscriber_id))
            if sub:
                prov_context.subscriber_code = getattr(sub, "account_number", "") or ""
                prov_context.subscriber_name = getattr(sub, "full_name", "") or ""
            break

    # Build spec and generate commands
    spec = build_spec_from_profile(
        profile, prov_context, tr069_profile_id=tr069_olt_profile_id, olt=olt
    )
    command_sets = [
        OltCommandSet(
            step=item.step,
            commands=[mask_credentials(command) for command in item.commands],
            description=item.description,
            requires_config_mode=item.requires_config_mode,
        )
        for item in HuaweiCommandGenerator.generate_full_provisioning(
            spec, prov_context
        )
    ]

    context["command_sets"] = command_sets
    context["spec"] = spec
    context["prov_context"] = prov_context

    return context
