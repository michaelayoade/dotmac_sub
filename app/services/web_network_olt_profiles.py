"""Web service for OLT profile display (line, service, TR-069, WAN profiles)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OntProvisioningProfile
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
