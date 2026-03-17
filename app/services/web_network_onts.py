"""Web service helpers for ONT form dropdowns and context."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    PonType,
    Splitter,
    Vlan,
)
from app.models.tr069 import Tr069AcsServer
from app.services.network.onu_types import onu_types
from app.services.network.speed_profiles import speed_profiles
from app.services.network.zones import network_zones

logger = logging.getLogger(__name__)


def get_onu_types(db: Session) -> list[Any]:
    """Fetch active ONU types for form dropdowns."""
    return onu_types.list(db, is_active=True)


def get_olt_devices(db: Session) -> list[OLTDevice]:
    """Fetch active OLT devices for form dropdowns."""
    stmt = (
        select(OLTDevice)
        .where(OLTDevice.is_active.is_(True))
        .order_by(OLTDevice.name)
    )
    return list(db.scalars(stmt).all())


def get_vlans(db: Session) -> list[Vlan]:
    """Fetch VLANs for form dropdowns."""
    stmt = select(Vlan).order_by(Vlan.tag)
    return list(db.scalars(stmt).all())


def get_zones(db: Session) -> list[Any]:
    """Fetch active network zones for form dropdowns."""
    return network_zones.list(db, is_active=True)


def get_splitters(db: Session) -> list[Splitter]:
    """Fetch splitters for form dropdowns."""
    stmt = (
        select(Splitter)
        .where(Splitter.is_active.is_(True))
        .order_by(Splitter.name)
    )
    return list(db.scalars(stmt).all())


def get_speed_profiles(db: Session, direction: str) -> list[Any]:
    """Fetch speed profiles for a given direction (download/upload)."""
    return speed_profiles.list(db, direction=direction, is_active=True)


def get_tr069_servers(db: Session) -> list[Tr069AcsServer]:
    """Fetch active TR069 ACS servers for form dropdowns."""
    stmt = (
        select(Tr069AcsServer)
        .where(Tr069AcsServer.is_active.is_(True))
        .order_by(Tr069AcsServer.name)
    )
    return list(db.scalars(stmt).all())


def get_provisioning_profiles(db: Session) -> list[Any]:
    """Fetch active ONT provisioning profiles for form dropdowns."""
    from app.models.network import OntProvisioningProfile

    stmt = (
        select(OntProvisioningProfile)
        .where(OntProvisioningProfile.is_active.is_(True))
        .order_by(OntProvisioningProfile.name)
    )
    return list(db.scalars(stmt).all())


def ont_form_dependencies(db: Session) -> dict[str, Any]:
    """Build all dropdown data needed by the ONT provisioning form."""
    return {
        "onu_types": get_onu_types(db),
        "olt_devices": get_olt_devices(db),
        "vlans": get_vlans(db),
        "zones": get_zones(db),
        "splitters": get_splitters(db),
        "speed_profiles_download": get_speed_profiles(db, "download"),
        "speed_profiles_upload": get_speed_profiles(db, "upload"),
        "pon_types": [e.value for e in PonType],
    }


# ---------------------------------------------------------------------------
# Bulk ONT Operations
# ---------------------------------------------------------------------------

_BULK_ACTIONS = {"reboot", "refresh", "factory_reset"}


def execute_bulk_action(
    db: Session,
    ont_ids: list[str],
    action: str,
) -> dict[str, Any]:
    """Execute a bulk action on multiple ONTs via TR-069.

    Args:
        db: Database session.
        ont_ids: List of OntUnit IDs.
        action: One of 'reboot', 'refresh', 'factory_reset'.

    Returns:
        Stats dict with succeeded/failed/skipped counts and per-ONT results.
    """
    from app.services.network.ont_actions import OntActions

    if action not in _BULK_ACTIONS:
        return {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "error": f"Invalid action: {action}",
            "results": [],
        }

    if not ont_ids:
        return {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "error": "No ONTs selected",
            "results": [],
        }

    # Cap at 50 to prevent accidental mass operations
    capped_ids = ont_ids[:50]
    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0

    for ont_id in capped_ids:
        try:
            if action == "reboot":
                result = OntActions.reboot(db, ont_id)
            elif action == "refresh":
                result = OntActions.refresh_status(db, ont_id)
            elif action == "factory_reset":
                result = OntActions.factory_reset(db, ont_id)
            else:
                continue

            if result.success:
                succeeded += 1
            else:
                failed += 1
            results.append({
                "ont_id": ont_id,
                "success": result.success,
                "message": result.message,
            })
        except Exception as exc:
            failed += 1
            results.append({
                "ont_id": ont_id,
                "success": False,
                "message": str(exc),
            })
            logger.error("Bulk %s failed for ONT %s: %s", action, ont_id, exc)

    skipped = len(ont_ids) - len(capped_ids)
    logger.info(
        "Bulk %s: %d succeeded, %d failed, %d skipped (of %d requested)",
        action, succeeded, failed, skipped, len(ont_ids),
    )
    return {
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "total": len(capped_ids),
        "results": results,
    }
