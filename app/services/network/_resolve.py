"""Shared GenieACS device resolution for ONT and CPE services."""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import CPEDevice, OLTDevice, OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import settings_spec
from app.services.genieacs_client import GenieACSClient, create_genieacs_client
from app.services.genieacs_client import GenieACSError, normalize_tr069_serial
from app.services.network.serial_utils import search_candidates

logger = logging.getLogger(__name__)


def _normalized_serial_expr(column):  # type: ignore[no-untyped-def]
    """Build a SQL expression that strips common serial formatting."""
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _serial_search_candidates(serial_number: str | None) -> list[str]:
    """Build likely serial variants for GenieACS/TR-069 lookup.

    Supports raw inventory serials like ``HWTC7D4701C3`` and Huawei hex-style
    serials like ``485754437D4701C3`` that GenieACS may report instead.
    """
    return search_candidates(serial_number)


def _normalized_serial_candidates(serial_number: str | None) -> list[str]:
    values = [
        normalize_tr069_serial(value) for value in search_candidates(serial_number)
    ]
    return [value for value in dict.fromkeys(values) if value]


def _resolve_unlinked_tr069_match(
    db: Session,
    ont: OntUnit,
    server: Tr069AcsServer | None,
    *,
    require_unlinked: bool = True,
) -> Tr069CpeDevice | None:
    """Find and link an observed TR-069 row that already matches this ONT."""
    serial_candidates = _normalized_serial_candidates(
        getattr(ont, "serial_number", None)
    )
    if not serial_candidates:
        return None

    conditions = [
        _normalized_serial_expr(Tr069CpeDevice.serial_number).in_(serial_candidates)
    ]
    for candidate in serial_candidates:
        conditions.append(Tr069CpeDevice.genieacs_device_id.ilike(f"%-{candidate}"))

    stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.genieacs_device_id.is_not(None))
        .where(
            (Tr069CpeDevice.acs_server_id == server.id)
            if server is not None
            else Tr069CpeDevice.acs_server_id.is_not(None)
        )
        .where(or_(*conditions))
        .limit(1)
    )
    if require_unlinked:
        stmt = stmt.where(Tr069CpeDevice.ont_unit_id.is_(None))
    device = db.scalars(stmt).first()
    if device is None:
        return None

    from app.services.tr069 import link_tr069_device_to_ont

    link_tr069_device_to_ont(
        db,
        device,
        ont,
        acs_server_id=server.id if server is not None else device.acs_server_id,
    )
    logger.info(
        "Linked ONT %s to existing TR-069 device %s while resolving GenieACS identity",
        ont.id,
        device.id,
    )
    return device


def clear_stale_genieacs_device_id(
    db: Session,
    ont: OntUnit,
    stale_device_id: str,
) -> bool:
    """Clear a stale linked GenieACS ID so serial search can rediscover it."""
    clean_id = str(stale_device_id or "").strip()
    if not clean_id:
        return False
    stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont.id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.genieacs_device_id == clean_id)
        .limit(1)
    )
    device = db.scalars(stmt).first()
    if device is None:
        return False
    device.genieacs_device_id = None
    try:
        db.flush()
    except Exception:
        db.rollback()
        logger.debug(
            "Failed to clear stale GenieACS device id %s for ONT %s",
            clean_id,
            ont.id,
            exc_info=True,
        )
        return False
    return True


def _resolve_olt_via_assignment(db: Session, ont: OntUnit) -> OLTDevice | None:
    """Fall back to active assignment → PON port → OLT resolution."""
    from app.models.network import PonPort

    for assignment in getattr(ont, "assignments", []):
        if not assignment.active or not assignment.pon_port_id:
            continue
        pon_port = db.get(PonPort, str(assignment.pon_port_id))
        if pon_port and pon_port.olt_id:
            olt = db.get(OLTDevice, str(pon_port.olt_id))
            if olt:
                return olt
    return None


def _resolve_device_id_from_server(client: GenieACSClient, serial_number: str) -> str | None:
    for candidate in _serial_search_candidates(serial_number):
        escaped_candidate = re.escape(candidate)
        devices = client.list_devices(
            query={
                "$or": [
                    {"_id": {"$regex": f".*-{escaped_candidate}$"}},
                    {"_deviceId._SerialNumber": candidate},
                    {"_deviceId.SerialNumber": candidate},
                    {"Device.DeviceInfo.SerialNumber._value": candidate},
                    {
                        "InternetGatewayDevice.DeviceInfo.SerialNumber._value": (
                            candidate
                        )
                    },
                ]
            }
        )
        if not devices:
            continue
        device_id = str(devices[0].get("_id") or "").strip()
        if device_id:
            return device_id
    return None


def _resolve_server_by_id(
    db: Session,
    server_id: str | None,
) -> Tr069AcsServer | None:
    if not server_id:
        return None
    server = db.get(Tr069AcsServer, str(server_id))
    if not server or not server.base_url:
        return None
    return server


def _cache_genieacs_device_id(
    db: Session,
    device: Tr069CpeDevice | None,
    device_id: str | None,
) -> None:
    if not device or not device_id:
        return
    clean_id = str(device_id).strip()
    if not clean_id or device.genieacs_device_id == clean_id:
        return

    owner = db.scalars(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.genieacs_device_id == clean_id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.id != device.id)
        .limit(1)
    ).first()
    if owner:
        if device.ont_unit_id and not owner.ont_unit_id:
            owner_id = owner.id
            placeholder_id = device.id
            ont_unit_id = device.ont_unit_id
            cpe_device_id = device.cpe_device_id
            device.ont_unit_id = None
            device.is_active = False
            try:
                db.flush()
                owner.ont_unit_id = ont_unit_id
                if owner.cpe_device_id is None and cpe_device_id is not None:
                    owner.cpe_device_id = cpe_device_id
                db.flush()
            except Exception:
                db.rollback()
                logger.debug(
                    "Failed to move ONT link from TR-069 placeholder %s to "
                    "GenieACS device %s",
                    placeholder_id,
                    owner_id,
                    exc_info=True,
                )
            return
        logger.info(
            "Skipping GenieACS device id cache for TR-069 device %s because %s "
            "is already assigned to active TR-069 device %s",
            device.id,
            clean_id,
            owner.id,
        )
        return

    device.genieacs_device_id = clean_id[:255]
    try:
        db.flush()
    except Exception:
        db.rollback()
        logger.debug(
            "Failed to cache GenieACS device id %s on TR-069 device %s",
            clean_id,
            device.id,
            exc_info=True,
        )


def resolve_genieacs(db: Session, ont: OntUnit) -> tuple[GenieACSClient, str] | None:
    resolved, _reason = resolve_genieacs_with_reason(db, ont)
    return resolved


def resolve_genieacs_with_reason(
    db: Session, ont: OntUnit
) -> tuple[tuple[GenieACSClient, str] | None, str]:
    """Resolve GenieACS client and device ID for an ONT.

    Resolution priority:
    1. Linked TR-069 device with genieacs_device_id (authoritative)
    2. Search GenieACS via OLT's ACS server
    3. Search GenieACS via default ACS server

    Returns:
        Tuple of (client, device_id) or None if not resolvable.
    """
    if not getattr(ont, "serial_number", None):
        return None, "ONT serial number is missing."

    from app.services.network.acs_resolution import resolve_acs_for_ont

    acs_resolution = resolve_acs_for_ont(db, ont)

    # 1) Primary: Linked TR-069 device with genieacs_device_id
    linked_stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont.id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .limit(1)
    )
    linked = db.scalars(linked_stmt).first()
    if linked and linked.acs_server_id and linked.genieacs_device_id:
        server = _resolve_server_by_id(db, str(linked.acs_server_id))
        if server:
            desired_server = acs_resolution.server
            if desired_server is None or linked.acs_server_id == desired_server.id:
                client = create_genieacs_client(server.base_url)
                return (
                    client,
                    linked.genieacs_device_id,
                ), "resolved_via_linked_tr069_device"
            logger.info(
                "Ignoring stale linked TR-069 device for ONT %s: linked ACS %s, desired ACS %s",
                ont.id,
                linked.acs_server_id,
                desired_server.id,
            )
    if linked and linked.acs_server_id:
        logger.debug(
            "Linked TR-069 placeholder for ONT %s has no GenieACS device id yet",
            ont.id,
        )

    servers_to_try = []
    if acs_resolution.server is not None:
        reason = {
            "ont_override": "resolved_via_ont_acs",
            "olt_default": "resolved_via_olt_acs",
            "system_default": "resolved_via_default_acs",
        }.get(acs_resolution.source, f"resolved_via_{acs_resolution.source}")
        servers_to_try.append((acs_resolution.server, reason))

    serial = str(getattr(ont, "serial_number", "") or "").strip()
    for server, _reason in servers_to_try:
        matched = _resolve_unlinked_tr069_match(
            db,
            ont,
            server,
            require_unlinked=False,
        )
        if matched and matched.genieacs_device_id:
            client = create_genieacs_client(server.base_url)
            return (
                client,
                str(matched.genieacs_device_id),
            ), "resolved_via_unlinked_tr069_serial_match"

    for server, reason in servers_to_try:
        client = create_genieacs_client(server.base_url)
        try:
            found_device_id = (
                _resolve_device_id_from_server(client, serial) if serial else None
            )
            if found_device_id:
                _cache_genieacs_device_id(db, linked, found_device_id)
                return (client, found_device_id), reason
        except GenieACSError:
            logger.debug(
                "GenieACS search failed for ONT %s on %s",
                ont.serial_number,
                server.name,
            )

    if not servers_to_try:
        return (
            None,
            "No ACS server configured. Set TR-069 ACS on OLT or configure default ACS server.",
        )
    return None, f"No TR-069 device found in GenieACS for ONT serial '{serial}'."


def resolve_genieacs_for_cpe(
    db: Session, cpe: CPEDevice
) -> tuple[GenieACSClient, str] | None:
    """Resolve GenieACS client and device ID for a CPE device."""
    resolved, _reason = resolve_genieacs_for_cpe_with_reason(db, cpe)
    return resolved


def resolve_genieacs_for_cpe_with_reason(
    db: Session, cpe: CPEDevice
) -> tuple[tuple[GenieACSClient, str] | None, str]:
    """Resolve GenieACS client and device ID for a CPE device.

    Resolution tiers (simpler than ONT — no OLT hierarchy):
    1. Linked Tr069CpeDevice by cpe_device_id FK
    2. Linked Tr069CpeDevice by normalized serial number match
    3. Default ACS server from settings

    Returns:
        Tuple of (client, device_id) or None if not resolvable.
    """
    serial = str(getattr(cpe, "serial_number", None) or "").strip()
    if not serial:
        return None, "CPE serial number is missing."

    cpe_id = str(cpe.id) if cpe.id else ""

    # 1) Linked Tr069CpeDevice by cpe_device_id FK
    if cpe_id:
        stmt = (
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.cpe_device_id == cpe.id)
            .where(Tr069CpeDevice.is_active.is_(True))
            .limit(1)
        )
        linked = db.scalars(stmt).first()
        if linked and linked.acs_server_id:
            server = _resolve_server_by_id(db, str(linked.acs_server_id))
            if server:
                client = create_genieacs_client(server.base_url)
                if linked.genieacs_device_id:
                    return (
                        client,
                        str(linked.genieacs_device_id),
                    ), "resolved_via_cpe_device_fk"
                try:
                    device_id = _resolve_device_id_from_server(client, serial)
                except GenieACSError:
                    device_id = None
                if device_id:
                    _cache_genieacs_device_id(db, linked, device_id)
                    return (client, device_id), "resolved_via_cpe_device_fk"

    # 2) Linked Tr069CpeDevice by normalized serial number match
    normalized_candidates = [
        normalize_tr069_serial(value) for value in _serial_search_candidates(serial)
    ]
    normalized_candidates = [value for value in normalized_candidates if value]
    if normalized_candidates:
        stmt = (
            select(Tr069CpeDevice)
            .where(
                _normalized_serial_expr(Tr069CpeDevice.serial_number).in_(
                    normalized_candidates
                )
            )
            .where(Tr069CpeDevice.is_active.is_(True))
            .limit(1)
        )
        cpe_tr069 = db.scalars(stmt).first()

        if cpe_tr069 and cpe_tr069.acs_server_id:
            server = _resolve_server_by_id(db, str(cpe_tr069.acs_server_id))
            if server:
                client = create_genieacs_client(server.base_url)
                if cpe_tr069.genieacs_device_id:
                    return (
                        client,
                        str(cpe_tr069.genieacs_device_id),
                    ), "resolved_via_tr069_serial_match"
                try:
                    device_id = _resolve_device_id_from_server(client, serial)
                except GenieACSError:
                    device_id = None
                if device_id:
                    _cache_genieacs_device_id(db, cpe_tr069, device_id)
                    return (client, device_id), "resolved_via_tr069_serial_match"

    # 3) Default ACS server from settings
    default_server_id = settings_spec.resolve_value(
        db,
        SettingDomain.tr069,
        "default_acs_server_id",
    )
    if default_server_id:
        server = _resolve_server_by_id(db, str(default_server_id))
        if server:
            client = create_genieacs_client(server.base_url)
            try:
                device_id = _resolve_device_id_from_server(client, serial)
                if device_id:
                    return (client, device_id), "resolved_via_default_acs"
            except GenieACSError:
                logger.warning("Failed to search GenieACS for CPE %s", serial)

    if not default_server_id:
        return (
            None,
            "No ACS server configured on linked TR-069 device or default settings.",
        )
    return None, f"No matching GenieACS device found for CPE serial '{serial}'."
