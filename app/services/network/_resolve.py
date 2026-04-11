"""Shared GenieACS device resolution for ONT and CPE services."""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import CPEDevice, OLTDevice, OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import settings_spec
from app.services.genieacs import GenieACSClient, GenieACSError, normalize_tr069_serial

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
    serial = str(serial_number or "").strip()
    if not serial:
        return []

    candidates: list[str] = []

    def add(value: str | None) -> None:
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(serial)
    normalized = normalize_tr069_serial(serial)
    add(normalized)

    # Huawei display serials often appear as HWTC-XXXXXXXX on OLTs but as
    # 48575443XXXXXXXX in GenieACS (ASCII vendor prefix hex-encoded).
    if len(normalized) == 12 and normalized[:4].isalpha():
        add(f"{normalized[:4]}-{normalized[4:]}")
        vendor_hex = normalized[:4].encode("ascii").hex().upper()
        add(vendor_hex + normalized[4:])

    # If the provided serial is already in Huawei hex form, also try the
    # human-readable vendor-prefix representation for local matching.
    if len(normalized) == 16 and re.fullmatch(r"[0-9A-F]{16}", normalized):
        try:
            vendor_ascii = bytes.fromhex(normalized[:8]).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            vendor_ascii = ""
        if vendor_ascii.isalpha():
            add(vendor_ascii + normalized[8:])
            add(f"{vendor_ascii}-{normalized[8:]}")

    return candidates


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


def _resolve_device_id_from_server(
    client: GenieACSClient,
    serial_number: str,
) -> str | None:
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
    device.genieacs_device_id = clean_id[:255]
    try:
        db.flush()
    except Exception:
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
            client = GenieACSClient(server.base_url)
            return (client, linked.genieacs_device_id), "resolved_via_linked_tr069_device"
    if linked and linked.acs_server_id:
        logger.debug(
            "Linked TR-069 placeholder for ONT %s has no GenieACS device id yet",
            ont.id,
        )

    # 2) Discovery: Search GenieACS for device by serial
    # Try OLT's ACS server first, then default server
    ont_server = None
    if ont.tr069_acs_server_id:
        ont_server = _resolve_server_by_id(db, str(ont.tr069_acs_server_id))
    olt_server = None
    olt = None
    if ont.olt_device_id:
        olt = db.get(OLTDevice, str(ont.olt_device_id))
    if not olt:
        olt = _resolve_olt_via_assignment(db, ont)
    if olt and olt.tr069_acs_server_id:
        olt_server = _resolve_server_by_id(db, str(olt.tr069_acs_server_id))

    # Also check default ACS server
    default_server_id = settings_spec.resolve_value(
        db,
        SettingDomain.tr069,
        "default_acs_server_id",
    )
    default_server = _resolve_server_by_id(db, str(default_server_id)) if default_server_id else None

    # Try servers in priority order
    servers_to_try = []
    if ont_server:
        servers_to_try.append((ont_server, "resolved_via_ont_acs"))
    if olt_server:
        servers_to_try.append((olt_server, "resolved_via_olt_acs"))
    if default_server and default_server not in {ont_server, olt_server}:
        servers_to_try.append((default_server, "resolved_via_default_acs"))

    serial = str(getattr(ont, "serial_number", "") or "").strip()
    for server, reason in servers_to_try:
        client = GenieACSClient(server.base_url)
        try:
            found_device_id = (
                _resolve_device_id_from_server(client, serial) if serial else None
            )
            if found_device_id:
                _cache_genieacs_device_id(db, linked, found_device_id)
                return (client, found_device_id), reason
        except GenieACSError:
            logger.debug("GenieACS search failed for ONT %s on %s", ont.serial_number, server.name)

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
                client = GenieACSClient(server.base_url)
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
                client = GenieACSClient(server.base_url)
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
            client = GenieACSClient(server.base_url)
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
