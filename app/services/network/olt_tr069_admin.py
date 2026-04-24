"""TR-069 admin helpers for OLT web flows.

This module owns ACS resolution, TR-069 profile matching/creation, ACS
propagation, and TR-069 profile admin actions. The active web entrypoints live
in the split OLT admin routers, while tests and services patch these public
helpers directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.tr069 import Tr069AcsServer
from app.services import settings_spec
from app.services.credential_crypto import decrypt_credential
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.tr069_profile_matching import match_tr069_profile

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
    if active_servers:
        logger.warning(
            "Multiple active ACS servers are configured without an OLT link or default ACS setting; refusing ambiguous fallback."
        )
    return None


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


def linked_acs_profile_payload(olt: OLTDevice) -> dict[str, str] | None:
    """Build the ACS payload used to create or match an OLT TR-069 profile."""
    server = getattr(olt, "tr069_acs_server", None)
    if not server or not server.cwmp_url:
        return None
    password = decrypt_credential(server.cwmp_password) if server.cwmp_password else ""
    raw_name = str(getattr(server, "name", "") or "ACS")
    profile_suffix = re.sub(r"[^A-Za-z0-9 ._-]+", " ", raw_name).strip()
    profile_suffix = re.sub(r"\s+", " ", profile_suffix) or "ACS"
    return {
        "profile_name": f"ACS {profile_suffix[:48]}",
        "acs_url": server.cwmp_url,
        "username": server.cwmp_username or "",
        "password": password or "",
    }


def ensure_tr069_profile_for_linked_acs(
    olt: OLTDevice,
) -> tuple[bool, str, int | None]:
    """Create or verify the TR-069 profile that matches the linked ACS."""
    payload = linked_acs_profile_payload(olt)
    if payload is None:
        logger.warning(
            "TR-069 profile ensure skipped: olt=%s olt_id=%s reason=no_linked_acs",
            olt.name,
            olt.id,
        )
        return False, "No linked TR-069 ACS server is configured for this OLT", None

    logger.info(
        "TR-069 profile ensure requested: olt=%s olt_id=%s acs_url=%s username_set=%s",
        olt.name,
        olt.id,
        payload["acs_url"],
        bool(payload["username"]),
    )
    ok, msg, profiles = olt_ssh_service.get_tr069_server_profiles(olt)
    if not ok:
        logger.warning(
            "TR-069 profile ensure failed while reading profiles: olt=%s olt_id=%s message=%s",
            olt.name,
            olt.id,
            msg,
        )
        return False, msg, None

    existing = match_tr069_profile(
        profiles,
        acs_url=payload["acs_url"],
        acs_username=payload["username"],
    )
    if existing is not None:
        logger.info(
            "TR-069 profile ensure matched existing profile: olt=%s olt_id=%s profile_id=%s profile_name=%s",
            olt.name,
            olt.id,
            existing.profile_id,
            existing.name,
        )
        return (
            True,
            f"TR-069 profile already exists: {existing.name} (ID {existing.profile_id})",
            existing.profile_id,
        )

    logger.info(
        "TR-069 profile ensure creating profile: olt=%s olt_id=%s profile_name=%s",
        olt.name,
        olt.id,
        payload["profile_name"],
    )
    ok, msg = olt_ssh_service.create_tr069_server_profile(
        olt,
        profile_name=payload["profile_name"],
        acs_url=payload["acs_url"],
        username=payload["username"],
        password=payload["password"],
        inform_interval=getattr(
            olt.tr069_acs_server,
            "periodic_inform_interval",
            settings.tr069_periodic_inform_interval,
        )
        or settings.tr069_periodic_inform_interval,
    )
    if not ok:
        logger.warning(
            "TR-069 profile ensure create failed: olt=%s olt_id=%s message=%s",
            olt.name,
            olt.id,
            msg,
        )
        return False, msg, None

    ok, msg, profiles = olt_ssh_service.get_tr069_server_profiles(olt)
    if not ok:
        logger.warning(
            "TR-069 profile ensure failed while verifying created profile: olt=%s olt_id=%s message=%s",
            olt.name,
            olt.id,
            msg,
        )
        return False, msg, None

    existing = match_tr069_profile(
        profiles,
        acs_url=payload["acs_url"],
        acs_username=payload["username"],
    )
    if existing is None:
        logger.warning(
            "TR-069 profile ensure created profile but could not verify it: olt=%s olt_id=%s profile_name=%s",
            olt.name,
            olt.id,
            payload["profile_name"],
        )
        return False, "Profile created but could not be verified on the OLT", None
    logger.info(
        "TR-069 profile ensure created and verified profile: olt=%s olt_id=%s profile_id=%s profile_name=%s",
        olt.name,
        olt.id,
        existing.profile_id,
        existing.name,
    )
    return True, msg, existing.profile_id


def ensure_tr069_profile_for_linked_acs_audited(
    db: Session,
    olt: OLTDevice,
    *,
    request: Request | None = None,
) -> tuple[bool, str, int | None]:
    ok, message, profile_id = ensure_tr069_profile_for_linked_acs(olt)
    log_olt_audit_event(
        db,
        request=request,
        action="init_tr069",
        entity_id=olt.id,
        metadata={"success": ok, "message": message},
    )
    return ok, message, profile_id


def ensure_tr069_profile_for_olt_audited(
    db: Session,
    olt_id: str,
    *,
    request: Request | None = None,
) -> tuple[bool, str, int | None]:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", None
    return ensure_tr069_profile_for_linked_acs_audited(db, olt, request=request)


def queue_acs_propagation(db: Session, olt: OLTDevice) -> dict[str, int]:
    """Push ACS ManagementServer parameters to all active ONTs under an OLT."""
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

    inform_interval = str(
        server.periodic_inform_interval or settings.tr069_periodic_inform_interval
    )
    acs_params: dict[str, str] = {
        "Device.ManagementServer.URL": server.cwmp_url,
        "Device.ManagementServer.PeriodicInformEnable": "true",
        "Device.ManagementServer.PeriodicInformInterval": inform_interval,
        "InternetGatewayDevice.ManagementServer.URL": server.cwmp_url,
        "InternetGatewayDevice.ManagementServer.PeriodicInformEnable": "true",
        "InternetGatewayDevice.ManagementServer.PeriodicInformInterval": (
            inform_interval
        ),
    }
    if server.cwmp_username:
        acs_params["Device.ManagementServer.Username"] = server.cwmp_username
        acs_params["InternetGatewayDevice.ManagementServer.Username"] = (
            server.cwmp_username
        )
    if server.cwmp_password:
        password = decrypt_credential(server.cwmp_password)
        if password:
            acs_params["Device.ManagementServer.Password"] = password
            acs_params["InternetGatewayDevice.ManagementServer.Password"] = password

    # Send both TR-098 (InternetGatewayDevice) and TR-181 (Device) parameters.
    # Devices will ignore unsupported paths, so sending both is safe and avoids
    # needing to detect the data model before propagation.

    for ont in onts:
        stats["attempted"] += 1
        try:
            resolved, reason = resolve_genieacs_with_reason(db, ont)
            if resolved:
                client, device_id = resolved
                # Fire and forget: send params without strict verification since
                # only one data model will apply (device ignores unsupported paths).
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
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", [], {}

    ok, message, profiles = olt_ssh_service.get_tr069_server_profiles(olt)
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
            & (OntAssignment.active.is_(True)),
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
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    if not acs_url:
        acs = resolve_operational_acs_server(db, olt=olt)
        if acs is None or not acs.cwmp_url:
            return False, "No operational ACS server is configured."
        acs_url = str(acs.cwmp_url or "").strip()
        username = username or str(acs.cwmp_username or "").strip()
        if not password and acs.cwmp_password:
            password = decrypt_credential(acs.cwmp_password) or ""

    return olt_ssh_service.create_tr069_server_profile(
        olt,
        profile_name=profile_name,
        acs_url=acs_url,
        username=username,
        password=password,
        inform_interval=inform_interval,
    )


def handle_create_tr069_profile_audited(
    db: Session,
    olt_id: str,
    *,
    profile_name: str,
    acs_url: str,
    username: str = "",
    password: str = "",
    inform_interval: int = 300,
    request: Request | None = None,
) -> tuple[bool, str]:
    ok, message = handle_create_tr069_profile(
        db,
        olt_id,
        profile_name=profile_name,
        acs_url=acs_url,
        username=username,
        password=password,
        inform_interval=inform_interval,
    )
    log_olt_audit_event(
        db,
        request=request,
        action="create_tr069_profile",
        entity_id=olt_id,
        metadata={"result": "success" if ok else "error", "profile_name": profile_name},
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return ok, message


def handle_rebind_tr069_profiles(
    db: Session,
    olt_id: str,
    ont_ids: list[str],
    target_profile_id: int,
) -> dict[str, int | list[str]]:
    """Rebind selected ONTs to a TR-069 server profile."""
    olt = get_olt_or_none(db, olt_id)
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
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        bind_result = get_protocol_adapter(olt).bind_tr069_profile(
            fsp,
            onu_index,
            profile_id=target_profile_id,
        )
        ok = bind_result.success
        msg = bind_result.message
        if ok:
            rebound += 1
            try:
                from app.services.network.ont_provision_steps import (
                    queue_wait_tr069_bootstrap,
                )

                wait_result = queue_wait_tr069_bootstrap(db, str(ont.id))
                logger.info(
                    "Queued TR-069 bootstrap wait after OLT rebind: olt_id=%s ont_id=%s serial=%s message=%s",
                    olt_id,
                    ont.id,
                    ont.serial_number,
                    wait_result.message,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to queue TR-069 bootstrap wait after OLT rebind: olt_id=%s ont_id=%s serial=%s error=%s",
                    olt_id,
                    ont.id,
                    ont.serial_number,
                    exc,
                )
        else:
            failed += 1
            errors_list.append(f"ONT {ont.serial_number}: {msg}")

    return {"rebound": rebound, "failed": failed, "errors": errors_list}


def handle_rebind_tr069_profiles_audited(
    db: Session,
    olt_id: str,
    ont_ids: list[str],
    target_profile_id: int,
    *,
    request: Request | None = None,
) -> dict[str, int | list[str]]:
    stats = handle_rebind_tr069_profiles(db, olt_id, ont_ids, target_profile_id)
    rebound_val = stats.get("rebound", 0)
    failed_val = stats.get("failed", 0)
    rebound = rebound_val if isinstance(rebound_val, int) else 0
    failed = failed_val if isinstance(failed_val, int) else 0
    ok = rebound > 0
    log_olt_audit_event(
        db,
        request=request,
        action="rebind_tr069_profiles",
        entity_id=olt_id,
        metadata={
            "result": "success" if ok else "error",
            "rebound": rebound,
            "failed": failed,
            "target_profile_id": target_profile_id,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return stats
