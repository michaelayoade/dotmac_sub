"""Provisioning profile resolution helpers."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntProvisioningProfile, OntUnit

logger = logging.getLogger(__name__)


def resolve_profile(
    db: Session,
    ont: OntUnit,
    profile_id: str | None = None,
) -> OntProvisioningProfile | None:
    """Resolve a provisioning profile for an ONT.

    Priority: explicit profile_id > ONT's assigned profile > first active profile.
    """
    selected_id = profile_id or (
        str(ont.provisioning_profile_id) if ont.provisioning_profile_id else None
    )
    if selected_id:
        profile = db.get(OntProvisioningProfile, selected_id)
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
    if not ont.olt_device_id:
        logger.warning(
            "ONT %s has no assigned profile and no OLT scope",
            ont.serial_number,
        )
        return None
    fallback = db.scalars(
        select(OntProvisioningProfile)
        .where(
            OntProvisioningProfile.is_active.is_(True),
            OntProvisioningProfile.olt_device_id == ont.olt_device_id,
        )
    ).first()
    if fallback:
        logger.warning(
            "ONT %s has no assigned profile - falling back to '%s'",
            ont.serial_number,
            fallback.name,
        )
    return fallback


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
