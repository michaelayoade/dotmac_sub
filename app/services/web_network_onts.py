"""Web service helpers for ONT form dropdowns and context."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.network import (
    IpPool,
    OLTDevice,
    OntUnit,
    PonType,
    Splitter,
    Vlan,
)
from app.models.tr069 import Tr069AcsServer
from app.services.common import coerce_uuid
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
        select(OLTDevice).where(OLTDevice.is_active.is_(True)).order_by(OLTDevice.name)
    )
    return list(db.scalars(stmt).all())


def get_vlans(db: Session) -> list[Vlan]:
    """Fetch VLANs for form dropdowns."""
    stmt = select(Vlan).order_by(Vlan.tag)
    return list(db.scalars(stmt).all())


def get_vlans_for_olt(
    db: Session,
    olt_device_id: str | None,
    *,
    include_vlan_ids: list[str] | None = None,
) -> list[Vlan]:
    """Fetch VLANs assigned to an OLT, preserving explicitly selected VLANs."""
    include_ids = [v for v in (include_vlan_ids or []) if v]
    if not olt_device_id and not include_ids:
        return []

    stmt = select(Vlan)
    if olt_device_id and include_ids:
        stmt = stmt.where(
            or_(
                Vlan.olt_device_id == olt_device_id,
                Vlan.id.in_(include_ids),
            )
        )
    elif olt_device_id:
        stmt = stmt.where(Vlan.olt_device_id == olt_device_id)
    else:
        stmt = stmt.where(Vlan.id.in_(include_ids))

    stmt = stmt.order_by(Vlan.tag)
    return list(db.scalars(stmt).all())


def get_vlans_for_ont(db: Session, ont: OntUnit | Any | None) -> list[Vlan]:
    """Fetch VLANs scoped to an ONT's OLT, keeping current selections visible."""
    if ont is None:
        return []

    selected_vlan_ids = [
        str(vlan_id)
        for vlan_id in [
            getattr(ont, "user_vlan_id", None),
            getattr(ont, "wan_vlan_id", None),
            getattr(ont, "mgmt_vlan_id", None),
        ]
        if vlan_id
    ]
    olt_device_id = getattr(ont, "olt_device_id", None)
    return get_vlans_for_olt(
        db,
        str(olt_device_id) if olt_device_id else None,
        include_vlan_ids=selected_vlan_ids,
    )


def get_tr069_profiles_for_ont(
    db: Session,
    ont: OntUnit | Any | None,
) -> tuple[list[Any], str | None]:
    """Fetch TR-069 server profiles for the ONT's assigned OLT."""
    if ont is None:
        return [], None

    olt_device_id = getattr(ont, "olt_device_id", None)
    if not olt_device_id:
        return [], "No OLT is assigned to this ONT"

    olt = db.get(OLTDevice, olt_device_id)
    if not olt:
        return [], "OLT not found"

    from app.services.network.olt_ssh_profiles import get_tr069_server_profiles

    ok, msg, profiles = get_tr069_server_profiles(olt)
    if ok:
        return profiles, None
    return [], msg


def get_zones(db: Session) -> list[Any]:
    """Fetch active network zones for form dropdowns."""
    return network_zones.list(db, is_active=True)


def get_splitters(db: Session) -> list[Splitter]:
    """Fetch splitters for form dropdowns."""
    stmt = select(Splitter).where(Splitter.is_active.is_(True)).order_by(Splitter.name)
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


def get_profile_templates(db: Session, olt_device_id: str | None = None) -> list[Any]:
    """Fetch active ONT profile templates for form dropdowns."""
    from app.models.network import OntProvisioningProfile

    stmt = (
        select(OntProvisioningProfile)
        .where(OntProvisioningProfile.is_active.is_(True))
        .order_by(OntProvisioningProfile.name)
    )
    if olt_device_id:
        olt_uuid = coerce_uuid(olt_device_id)
        stmt = stmt.where(OntProvisioningProfile.olt_device_id == olt_uuid)
    return list(db.scalars(stmt).all())


def ont_form_dependencies(
    db: Session, ont: OntUnit | Any | None = None
) -> dict[str, Any]:
    """Build all dropdown data needed by the ONT configuration form."""
    return {
        "onu_types": get_onu_types(db),
        "olt_devices": get_olt_devices(db),
        "vlans": get_vlans_for_ont(db, ont),
        "zones": get_zones(db),
        "splitters": get_splitters(db),
        "speed_profiles_download": get_speed_profiles(db, "download"),
        "speed_profiles_upload": get_speed_profiles(db, "upload"),
        "pon_types": [e.value for e in PonType],
    }


# ---------------------------------------------------------------------------
# Firmware Images
# ---------------------------------------------------------------------------


def get_active_firmware_images(
    db: Session,
    *,
    vendor_contains: str | None = None,
    limit: int | None = None,
) -> list:
    """Return active ONT firmware images for ONT UI workflows."""
    from sqlalchemy import select

    from app.models.network import OntFirmwareImage

    stmt = (
        select(OntFirmwareImage)
        .where(OntFirmwareImage.is_active.is_(True))
        .order_by(OntFirmwareImage.vendor, OntFirmwareImage.version.desc())
    )
    if vendor_contains:
        stmt = stmt.where(OntFirmwareImage.vendor.ilike(f"%{vendor_contains}%"))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Bulk ONT Operations
# ---------------------------------------------------------------------------

_BULK_ACTIONS = {
    "reboot",
    "refresh",
    "factory_reset",
    "firmware_upgrade",
    "return_to_inventory",
}


def execute_bulk_action(
    db: Session,
    ont_ids: list[str],
    action: str,
    *,
    firmware_image_id: str | None = None,
) -> dict[str, Any]:
    """Execute a bulk action on multiple ONTs.

    Args:
        db: Database session.
        ont_ids: List of OntUnit IDs.
        action: One of 'reboot', 'refresh', 'factory_reset',
            'firmware_upgrade', 'return_to_inventory'.
        firmware_image_id: Required when action is 'firmware_upgrade'.

    Returns:
        Stats dict with succeeded/failed/skipped counts and per-ONT results.
    """
    from app.services.network.ont_actions import OntActions
    from app.services.web_network_ont_actions import return_to_inventory

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
            elif action == "firmware_upgrade" and firmware_image_id:
                result = OntActions.firmware_upgrade(db, ont_id, firmware_image_id)
            elif action == "return_to_inventory":
                result = return_to_inventory(db, ont_id)
            else:
                continue

            if result.success:
                succeeded += 1
            else:
                failed += 1
            results.append(
                {
                    "ont_id": ont_id,
                    "success": result.success,
                    "message": result.message,
                }
            )
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "ont_id": ont_id,
                    "success": False,
                    "message": str(exc),
                }
            )
            logger.error("Bulk %s failed for ONT %s: %s", action, ont_id, exc)

    skipped = len(ont_ids) - len(capped_ids)
    logger.info(
        "Bulk %s: %d succeeded, %d failed, %d skipped (of %d requested)",
        action,
        succeeded,
        failed,
        skipped,
        len(ont_ids),
    )
    return {
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "total": len(capped_ids),
        "results": results,
    }


def bulk_action_summary_context(
    db: Session,
    ont_ids: list[str],
    action: str,
    *,
    firmware_image_id: str | None = None,
) -> dict[str, Any]:
    """Execute a bulk ONT action and return display-ready summary data."""
    stats = execute_bulk_action(
        db,
        ont_ids,
        action,
        firmware_image_id=firmware_image_id,
    )
    error = stats.get("error")
    skipped = int(stats.get("skipped", 0) or 0)
    return {
        "stats": stats,
        "action": action,
        "error": error,
        "skipped_text": f", {skipped} skipped (max 50)" if skipped else "",
    }


# ---------------------------------------------------------------------------
# Provisioning profile helpers
# ---------------------------------------------------------------------------


def get_provisioning_profiles(
    db: Session, olt_device_id: str | None = None
) -> list[Any]:
    """Fetch active ONT provisioning profiles for form dropdowns."""
    return get_profile_templates(db, olt_device_id=olt_device_id)


def provision_wizard_context(request: Any, db: Session, ont_id: str) -> dict[str, Any]:
    """Build template context for the ONT provisioning wizard page."""
    from app.services import network as network_service
    from app.services import web_admin as web_admin_service
    from app.services.network.ont_service_intent import load_ont_plan_for_ont
    from app.services.web_network_onts_provisioning import (
        validate_provision_form_fields,
    )

    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except Exception:
        return {"error": "ONT not found", "request": request}

    olt = getattr(ont, "olt_device", None)
    profile = resolve_effective_provisioning_profile(db, ont, olt)
    tr069_profile, tr069_error = resolve_effective_tr069_profile_for_ont(db, ont)
    tr069_profiles, tr069_profiles_error = get_tr069_profiles_for_ont(db, ont)
    vlans = get_vlans_for_ont(db, ont)
    ip_pools = list(
        db.scalars(
            select(IpPool)
            .where(IpPool.is_active.is_(True))
            .where(
                or_(
                    IpPool.olt_device_id == getattr(ont, "olt_device_id", None),
                    IpPool.olt_device_id.is_(None),
                )
            )
            .order_by(IpPool.name.asc())
        ).all()
    )
    vlan_nas_map = {
        str(vlan.id): str(vlan.olt_device_id)
        for vlan in vlans
        if getattr(vlan, "olt_device_id", None)
    }
    pool_nas_map = {
        str(pool.id): str(pool.olt_device_id)
        for pool in ip_pools
        if getattr(pool, "olt_device_id", None)
    }
    ont_plan = load_ont_plan_for_ont(db, ont_id=ont_id)
    lan_intent = (
        ont_plan.get("configure_lan_tr069")
        if isinstance(ont_plan.get("configure_lan_tr069"), dict)
        else {}
    )
    wifi_intent = (
        ont_plan.get("configure_wifi_tr069")
        if isinstance(ont_plan.get("configure_wifi_tr069"), dict)
        else {}
    )
    mgmt_mode = (
        ont.mgmt_ip_mode.value
        if getattr(ont, "mgmt_ip_mode", None) is not None
        else "dhcp"
    )
    wan_protocol = (
        ont.wan_mode.value
        if getattr(ont, "wan_mode", None) is not None
        else "pppoe"
    )
    if wan_protocol == "static_ip":
        wan_protocol = "static"
    elif wan_protocol == "bridge":
        wan_protocol = "bridged"

    provision_gate_issues = validate_provision_form_fields(
        profile_id=str(profile.id) if profile else None,
        onu_mode=ont.onu_mode.value if ont.onu_mode else "routing",
        mgmt_vlan_id=str(ont.mgmt_vlan_id) if ont.mgmt_vlan_id else None,
        mgmt_ip_mode=mgmt_mode,
        mgmt_ip_address=ont.mgmt_ip_address,
        mgmt_subnet=None,
        mgmt_gateway=None,
        wan_protocol=wan_protocol,
        wan_vlan_id=str(ont.wan_vlan_id) if ont.wan_vlan_id else None,
        pppoe_username=ont.pppoe_username,
        static_ip_pool_id=None,
        static_ip=None,
        static_subnet=None,
        static_gateway=None,
        static_dns=None,
        lan_ip=str(lan_intent.get("lan_ip") or "") or None,
        lan_subnet=str(lan_intent.get("lan_subnet") or "") or None,
        dhcp_enabled=(
            bool(lan_intent.get("dhcp_enabled"))
            if lan_intent.get("dhcp_enabled") is not None
            else None
        ),
        dhcp_start=str(lan_intent.get("dhcp_start") or "") or None,
        dhcp_end=str(lan_intent.get("dhcp_end") or "") or None,
        wifi_enabled=(
            bool(wifi_intent.get("enabled"))
            if wifi_intent.get("enabled") is not None
            else None
        ),
        wifi_ssid=str(wifi_intent.get("ssid") or "") or None,
        wifi_password=None,
    )

    context: dict[str, Any] = {
        "request": request,
        "active_page": "onts",
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
        "ont": ont,
        "olt": olt,
        "provisioning_profile": profile,
        "tr069_profile": tr069_profile,
        "tr069_profile_error": tr069_error,
        "selected_profile_id": str(profile.id) if profile else "",
        "selected_tr069_profile_id": getattr(tr069_profile, "profile_id", None),
        "selected_tr069_profile_name": getattr(tr069_profile, "name", None)
        or getattr(tr069_profile, "profile_name", None),
        "resolved_tr069_profile_error": tr069_error,
        "tr069_profiles": tr069_profiles,
        "tr069_profiles_error": tr069_profiles_error,
        "profiles": get_profile_templates(
            db, str(ont.olt_device_id) if ont.olt_device_id else None
        ),
        "vlans": vlans,
        "ip_pools": ip_pools,
        "vlan_nas_map": vlan_nas_map,
        "pool_nas_map": pool_nas_map,
        "tr069_servers": get_tr069_servers(db),
        "speed_profiles_download": get_speed_profiles(db, "download"),
        "speed_profiles_upload": get_speed_profiles(db, "upload"),
        "signal_info": {
            "online_status": getattr(
                getattr(ont, "effective_status", None),
                "value",
                getattr(ont, "online_status", "unknown"),
            ),
            "olt_rx_dbm": getattr(ont, "olt_rx_signal_dbm", None),
        },
        "pon_label": (
            f"{ont.board}/{ont.port}"
            if getattr(ont, "board", None) and getattr(ont, "port", None)
            else None
        ),
        "subscriber": None,
        "subscription": None,
        "acs_bound": bool(
            getattr(ont, "tr069_acs_server_id", None)
            or getattr(olt, "tr069_acs_server_id", None)
        ),
        "operational_acs_server_name": getattr(
            getattr(olt, "tr069_acs_server", None), "name", None
        ),
        "pppoe_username": getattr(ont, "pppoe_username", None),
        "ont_plan": ont_plan,
        "provision_gate_issues": provision_gate_issues,
    }
    return context


def resolve_effective_provisioning_profile(
    db: Session, ont: Any, olt: Any | None = None
) -> Any | None:
    """Resolve the provisioning profile for an ONT.

    Checks ONT-level override, then OLT default, then returns None.
    """
    from app.models.network import OntProvisioningProfile

    profile_id = getattr(ont, "provisioning_profile_id", None)
    if not profile_id and olt:
        profile_id = getattr(olt, "default_provisioning_profile_id", None)
    if profile_id:
        return db.get(OntProvisioningProfile, str(profile_id))
    return None


def resolve_effective_tr069_profile_for_ont(
    db: Session, ont: Any
) -> tuple[Any | None, str | None]:
    """Resolve the TR-069 OLT profile for an ONT.

    Returns (profile_data, error_message). profile_data may be a namespace
    with ``profile_id`` and ``profile_name`` attributes if found, or None.
    """
    profiles = get_tr069_profiles_for_ont(db, ont)
    if profiles:
        return profiles[0], None
    return None, "No TR-069 profile found for this ONT"
