"""Provisioning profile resolution helpers."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.network import OntProvisioningProfile, OntUnit
from app.services.network.ont_bundle_assignments import resolve_assigned_bundle

logger = logging.getLogger(__name__)


def resolve_profile(
    db: Session,
    ont: OntUnit,
    bundle_id: str | None = None,
) -> OntProvisioningProfile | None:
    """Resolve a provisioning profile for an ONT.

    Priority: explicit bundle_id > ONT's active assigned bundle.
    """
    if bundle_id:
        profile = db.get(OntProvisioningProfile, bundle_id)
        if (
            profile
            and profile.olt_device_id
            and profile.olt_device_id != ont.olt_device_id
        ):
            logger.warning(
                "ONT %s selected profile '%s' is scoped to OLT %s, not %s",
                ont.serial_number,
                profile.name,
                profile.olt_device_id,
                ont.olt_device_id,
            )
            return None
        return profile
    profile = resolve_assigned_bundle(db, ont)
    if profile is not None:
        return profile
    logger.warning(
        "ONT %s has no selected or assigned provisioning profile; refusing implicit profile fallback",
        ont.serial_number,
    )
    return None


def profile_requires_tr069(profile: OntProvisioningProfile | None) -> bool:
    """Check whether a profile's configuration requires TR-069."""
    if profile is None:
        return False
    if getattr(profile, "cr_username", None) or getattr(profile, "cr_password", None):
        return True
    if getattr(getattr(profile, "ip_protocol", None), "value", None) == "dual_stack":
        return True

    wan_services = getattr(profile, "wan_services", []) or []
    has_pppoe = any(
        (
            getattr(getattr(service, "connection_type", None), "value", None)
            or str(getattr(service, "connection_type", "") or "")
        )
        == "pppoe"
        for service in wan_services
        if getattr(service, "is_active", True)
    )
    omci_vlan = getattr(profile, "pppoe_omci_vlan", None)
    return has_pppoe and omci_vlan is None
