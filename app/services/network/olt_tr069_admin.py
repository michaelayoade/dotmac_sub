"""TR-069 admin helpers for OLT web flows.

This module owns ACS resolution, TR-069 profile matching/creation, ACS
propagation, and TR-069 profile admin actions. `web_network_olts.py` keeps
thin wrappers for compatibility so existing routes and tests can continue to
patch the same public names.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.tr069 import Tr069AcsServer
from app.services import settings_spec

logger = logging.getLogger(__name__)

_ONU_INDEX_RE = re.compile(r"\.(\d+)$")
_ONU_NAME_RE = re.compile(r":(\d+)$")


def resolve_operational_acs_server(
    db: Session,
    *,
    olt: OLTDevice | None = None,
) -> Tr069AcsServer | None:
    """Resolve the single ACS operators should use on OLT/ONT pages."""
    if olt is not None and getattr(olt, "tr069_acs_server", None) is not None:
        server = olt.tr069_acs_server
        if server and server.is_active and server.base_url:
            return server

    if olt is not None and olt.tr069_acs_server_id:
        server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
        if server and server.is_active and server.base_url:
            return server

    default_server_id = settings_spec.resolve_value(
        db, SettingDomain.tr069, "default_acs_server_id"
    )
    if default_server_id:
        server = db.get(Tr069AcsServer, str(default_server_id))
        if server and server.is_active and server.base_url:
            return server

    active_servers = list(
        db.scalars(
            select(Tr069AcsServer)
            .where(Tr069AcsServer.is_active.is_(True))
            .order_by(Tr069AcsServer.name)
        ).all()
    )
    if len(active_servers) == 1:
        return active_servers[0]
    return active_servers[0] if active_servers else None


def apply_default_acs_server(
    db: Session,
    values: dict[str, object],
    *,
    current_olt: OLTDevice | None = None,
) -> None:
    if values.get("tr069_acs_server_id"):
        return
    server = resolve_operational_acs_server(db, olt=current_olt)
    if server is not None:
        values["tr069_acs_server_id"] = str(server.id)


def normalize_acs_url(value: str | None) -> str:
    """Normalize ACS URLs for profile matching."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    if scheme and netloc:
        return f"{scheme}://{netloc}{path}"
    return raw.rstrip("/").lower()


def acs_host(value: str | None) -> str:
    """Extract the ACS host from a URL or raw host string."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    if host:
        return host.lower()
    return raw.split("//", 1)[-1].split(":", 1)[0].split("/", 1)[0].lower()


def match_tr069_profile(
    profiles: list[Any],
    *,
    acs_url: str,
    acs_username: str = "",
) -> Any | None:
    """Find the OLT TR-069 profile that best matches the target ACS."""
    normalized_url = normalize_acs_url(acs_url)
    target_host = acs_host(acs_url)
    normalized_username = str(acs_username or "").strip()

    for profile in profiles:
        profile_url = normalize_acs_url(getattr(profile, "acs_url", None))
        if not profile_url or profile_url != normalized_url:
            continue
        profile_username = str(getattr(profile, "acs_username", "") or "").strip()
        if not normalized_username or not profile_username:
            return profile
        if profile_username == normalized_username:
            return profile

    if target_host:
        for profile in profiles:
            profile_url = normalize_acs_url(getattr(profile, "acs_url", None))
            if profile_url and acs_host(profile_url) == target_host:
                return profile

    for profile in profiles:
        if "dotmac" in str(getattr(profile, "name", "") or "").lower():
            return profile
    return None


def linked_acs_profile_payload(olt: OLTDevice) -> dict[str, str] | None:
    """Build the ACS payload used to create or match an OLT TR-069 profile."""
    from app.services import web_network_olts as web_network_olts_service

    server = getattr(olt, "tr069_acs_server", None)
    if not server or not server.cwmp_url:
        return None
    password = (
        web_network_olts_service.decrypt_credential(server.cwmp_password)
        if server.cwmp_password
        else ""
    )
    return {
        "profile_name": "DotMac-ACS",
        "acs_url": server.cwmp_url,
        "username": server.cwmp_username or "",
        "password": password or "",
    }


def ensure_tr069_profile_for_linked_acs(
    olt: OLTDevice,
) -> tuple[bool, str, int | None]:
    """Create or verify the TR-069 profile that matches the linked ACS."""
    from app.services import web_network_olts as web_network_olts_service

    payload = linked_acs_profile_payload(olt)
    if payload is None:
        return False, "No linked TR-069 ACS server is configured for this OLT", None

    ok, msg, profiles = web_network_olts_service.olt_ssh_service.get_tr069_server_profiles(
        olt
    )
    if not ok:
        return False, msg, None

    existing = match_tr069_profile(
        profiles,
        acs_url=payload["acs_url"],
        acs_username=payload["username"],
    )
    if existing is not None:
        return (
            True,
            f"TR-069 profile already exists: {existing.name} (ID {existing.profile_id})",
            existing.profile_id,
        )

    ok, msg = web_network_olts_service.olt_ssh_service.create_tr069_server_profile(
        olt,
        profile_name=payload["profile_name"],
        acs_url=payload["acs_url"],
        username=payload["username"],
        password=payload["password"],
        inform_interval=300,
    )
    if not ok:
        return False, msg, None

    ok, msg, profiles = web_network_olts_service.olt_ssh_service.get_tr069_server_profiles(
        olt
    )
    if not ok:
        return False, msg, None

    existing = match_tr069_profile(
        profiles,
        acs_url=payload["acs_url"],
        acs_username=payload["username"],
    )
    if existing is None:
        return False, "Profile created but could not be verified on the OLT", None
    return True, msg, existing.profile_id


def queue_acs_propagation(db: Session, olt: OLTDevice) -> dict[str, int]:
    """Push ACS ManagementServer parameters to all active ONTs under an OLT."""
    from app.services import web_network_olts as web_network_olts_service
    from app.services.network._resolve import resolve_genieacs_with_reason

    stats = {
        "attempted": 0,
        "propagated": 0,
        "unresolved": 0,
        "errors": 0,
    }

    if not olt.tr069_acs_server_id:
        return stats
    server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
    if not server or not server.cwmp_url:
        return stats

    onts = list(
        db.scalars(
            select(OntUnit)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        ).all()
    )
    if not onts:
        return stats

    acs_params: dict[str, str] = {
        "Device.ManagementServer.URL": server.cwmp_url,
        "Device.ManagementServer.PeriodicInformEnable": "true",
        "Device.ManagementServer.PeriodicInformInterval": "3600",
        "InternetGatewayDevice.ManagementServer.URL": server.cwmp_url,
        "InternetGatewayDevice.ManagementServer.PeriodicInformEnable": "true",
        "InternetGatewayDevice.ManagementServer.PeriodicInformInterval": "3600",
    }
    if server.cwmp_username:
        acs_params["Device.ManagementServer.Username"] = server.cwmp_username
        acs_params["InternetGatewayDevice.ManagementServer.Username"] = (
            server.cwmp_username
        )
    if server.cwmp_password:
        password = web_network_olts_service.decrypt_credential(server.cwmp_password)
        if password:
            acs_params["Device.ManagementServer.Password"] = password
            acs_params["InternetGatewayDevice.ManagementServer.Password"] = password

    for ont in onts:
        stats["attempted"] += 1
        try:
            resolved, reason = resolve_genieacs_with_reason(db, ont)
            if resolved:
                client, device_id = resolved
                client.set_parameter_values(device_id, acs_params)
                logger.info("Propagated ACS config to ONT %s", ont.serial_number)
                stats["propagated"] += 1
            else:
                stats["unresolved"] += 1
                logger.info(
                    "Skipped ACS propagation for ONT %s: %s",
                    ont.serial_number,
                    reason,
                )
        except Exception as exc:
            logger.error(
                "Failed to propagate ACS to ONT %s: %s", ont.serial_number, exc
            )
            stats["errors"] += 1

    return stats


def extract_onu_index(ont: OntUnit) -> int | None:
    """Extract the ONU index from an ONT external_id or name."""
    if ont.external_id:
        match = _ONU_INDEX_RE.search(ont.external_id)
        if match:
            return int(match.group(1))
    if ont.name:
        match = _ONU_NAME_RE.search(ont.name)
        if match:
            return int(match.group(1))
    return None


def get_tr069_profiles_context(
    db: Session, olt_id: str
) -> tuple[bool, str, list[dict[str, Any]], dict[str, Any]]:
    """Read TR-069 server profiles from an OLT and prepare template context."""
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", [], {}

    ok, message, profiles = web_network_olts_service.olt_ssh_service.get_tr069_server_profiles(
        olt
    )
    profiles_data = [
        {
            "profile_id": profile.profile_id,
            "name": profile.name,
            "acs_url": profile.acs_url,
            "acs_username": profile.acs_username,
            "inform_interval": profile.inform_interval,
            "binding_count": profile.binding_count,
        }
        for profile in profiles
    ]

    acs_prefill: dict[str, Any] = {}
    acs = resolve_operational_acs_server(db, olt=olt)
    if acs is not None:
        acs_prefill = {
            "acs_url": acs.cwmp_url or "",
            "acs_username": acs.cwmp_username or "",
            "acs_name": acs.name or "",
        }

    stmt = (
        select(OntUnit)
        .outerjoin(
            OntAssignment,
            (OntAssignment.ont_unit_id == OntUnit.id)
            & (OntAssignment.is_active.is_(True)),
        )
        .outerjoin(PonPort, PonPort.id == OntAssignment.pon_port_id)
        .where(
            or_(
                OntUnit.olt_device_id == olt.id,
                PonPort.olt_id == olt.id,
            )
        )
        .distinct()
        .order_by(OntUnit.board, OntUnit.port, OntUnit.name)
    )
    onts = db.scalars(stmt).all()
    ont_rows = []
    for ont in onts:
        onu_index = extract_onu_index(ont)
        if onu_index is None:
            continue
        ont_rows.append(
            {
                "id": str(ont.id),
                "serial_number": ont.serial_number,
                "board": ont.board or "",
                "port": ont.port or "",
                "onu_index": onu_index,
                "name": ont.name or "",
                "online": ont.online_status.value if ont.online_status else "unknown",
                "subscriber_name": getattr(ont, "address_or_comment", "") or "",
            }
        )

    return ok, message, profiles_data, {"acs_prefill": acs_prefill, "onts": ont_rows}


def handle_create_tr069_profile(
    db: Session,
    olt_id: str,
    *,
    profile_name: str,
    acs_url: str,
    username: str = "",
    password: str = "",
    inform_interval: int = 300,
) -> tuple[bool, str]:
    """Validate and create a TR-069 server profile on an OLT."""
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    if not acs_url:
        acs = resolve_operational_acs_server(db, olt=olt)
        if acs is None or not acs.cwmp_url:
            return False, "No operational ACS server is configured."
        acs_url = str(acs.cwmp_url or "").strip()
        username = username or str(acs.cwmp_username or "").strip()
        if not password and acs.cwmp_password:
            password = (
                web_network_olts_service.decrypt_credential(acs.cwmp_password) or ""
            )

    return web_network_olts_service.olt_ssh_service.create_tr069_server_profile(
        olt,
        profile_name=profile_name,
        acs_url=acs_url,
        username=username,
        password=password,
        inform_interval=inform_interval,
    )


def handle_rebind_tr069_profiles(
    db: Session,
    olt_id: str,
    ont_ids: list[str],
    target_profile_id: int,
) -> dict[str, int | list[str]]:
    """Rebind selected ONTs to a TR-069 server profile."""
    from app.services import web_network_olts as web_network_olts_service

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return {"rebound": 0, "failed": 1, "errors": ["OLT not found"]}

    rebound = 0
    failed = 0
    errors_list: list[str] = []

    for ont_id in ont_ids:
        ont = db.get(OntUnit, ont_id)
        if not ont:
            errors_list.append(f"ONT {ont_id} not found")
            failed += 1
            continue

        onu_index = extract_onu_index(ont)
        if onu_index is None:
            errors_list.append(f"ONT {ont.serial_number}: cannot determine ONU index")
            failed += 1
            continue

        board = ont.board or ""
        port = ont.port or ""
        if not board or not port:
            errors_list.append(f"ONT {ont.serial_number}: missing board/port")
            failed += 1
            continue

        fsp = f"{board}/{port}"
        ok, msg = web_network_olts_service.olt_ssh_service.bind_tr069_server_profile(
            olt, fsp, onu_index, target_profile_id
        )
        if ok:
            rebound += 1
        else:
            failed += 1
            errors_list.append(f"ONT {ont.serial_number}: {msg}")

    return {"rebound": rebound, "failed": failed, "errors": errors_list}
