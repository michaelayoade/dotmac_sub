"""Web service for OLT profile display (line, service, TR-069, WAN profiles)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntProvisioningProfile
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OntProvisioningContext,
    build_spec_from_profile,
)
from app.services.network.olt_ssh_profiles import (
    get_line_profiles,
    get_service_profiles,
    get_tr069_server_profiles,
)
from app.services.web_network_service_ports import _resolve_ont_olt_context

logger = logging.getLogger(__name__)


def line_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT line and service profiles via SSH.

    Args:
        db: Database session.
        olt_id: OLTDevice ID.

    Returns:
        Context dict with line_profiles, service_profiles, error.
    """
    olt = db.get(OLTDevice, olt_id)
    if not olt:
        return {"error": "OLT not found", "line_profiles": [], "service_profiles": []}

    context: dict[str, Any] = {
        "olt": olt,
        "line_profiles": [],
        "service_profiles": [],
        "error": None,
    }

    ok, msg, profiles = get_line_profiles(olt)
    if ok:
        context["line_profiles"] = profiles
    else:
        context["error"] = msg

    ok2, msg2, svc_profiles = get_service_profiles(olt)
    if ok2:
        context["service_profiles"] = svc_profiles
    elif not context["error"]:
        context["error"] = msg2

    return context


def tr069_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT TR-069 server profiles via SSH.

    Args:
        db: Database session.
        olt_id: OLTDevice ID.

    Returns:
        Context dict with tr069_profiles, error.
    """
    olt = db.get(OLTDevice, olt_id)
    if not olt:
        return {"error": "OLT not found", "tr069_profiles": []}

    context: dict[str, Any] = {
        "olt": olt,
        "tr069_profiles": [],
        "error": None,
    }

    ok, msg, profiles = get_tr069_server_profiles(olt)
    if ok:
        context["tr069_profiles"] = profiles
    else:
        context["error"] = msg

    return context


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
        context["error"] = "Cannot resolve OLT context — check assignment and external ID"
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
        profile, prov_context, tr069_profile_id=tr069_olt_profile_id
    )
    command_sets = HuaweiCommandGenerator.generate_full_provisioning(spec, prov_context)

    context["command_sets"] = command_sets
    context["spec"] = spec
    context["prov_context"] = prov_context

    return context
