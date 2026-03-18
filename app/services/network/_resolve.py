"""Shared GenieACS device resolution for ONT services."""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntUnit
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
    serial = str(serial_number or "").strip()
    devices = client.list_devices(query={"_id": {"$regex": f".*-{re.escape(serial)}$"}})
    if not devices:
        normalized_target = normalize_tr069_serial(serial)
        if not normalized_target:
            return None
        devices = client.list_devices(
            query={"_id": {"$regex": f".*-{re.escape(normalized_target)}$"}}
        )
        if not devices:
            return None
    device_id = str(devices[0].get("_id") or "").strip()
    return device_id or None


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


def resolve_genieacs(
    db: Session, ont: OntUnit
) -> tuple[GenieACSClient, str] | None:
    resolved, _reason = resolve_genieacs_with_reason(db, ont)
    return resolved


def resolve_genieacs_with_reason(
    db: Session, ont: OntUnit
) -> tuple[tuple[GenieACSClient, str] | None, str]:
    """Resolve GenieACS client and device ID for an ONT.

    Looks up the TR-069 CPE device matching the ONT serial number,
    then builds the GenieACS device ID and client.

    Returns:
        Tuple of (client, device_id) or None if not resolvable.
    """
    if not getattr(ont, "serial_number", None):
        return None, "ONT serial number is missing."

    # 1) OLT profile (authoritative in inherited ACS model)
    #    Try ont.olt_device_id first, then fall back to assignment → PON port → OLT
    olt_server = None
    olt = None
    if ont.olt_device_id:
        olt = db.get(OLTDevice, str(ont.olt_device_id))
    if not olt:
        olt = _resolve_olt_via_assignment(db, ont)
    if olt and olt.tr069_acs_server_id:
        olt_server = _resolve_server_by_id(db, str(olt.tr069_acs_server_id))
    if olt_server:
        client = GenieACSClient(olt_server.base_url)
        try:
            device_id = _resolve_device_id_from_server(client, ont.serial_number)
            if device_id:
                return (client, device_id), "resolved_via_olt_acs"
        except GenieACSError:
            logger.warning("Failed OLT ACS lookup for ONT %s", ont.serial_number)

    # 2) ONT-level ACS server (optional per-device override)
    ont_server = _resolve_server_by_id(db, str(getattr(ont, "tr069_acs_server_id", "") or ""))
    if ont_server:
        client = GenieACSClient(ont_server.base_url)
        try:
            device_id = _resolve_device_id_from_server(client, ont.serial_number)
            if device_id:
                return (client, device_id), "resolved_via_ont_acs"
        except GenieACSError:
            logger.warning("Failed ONT ACS lookup for ONT %s", ont.serial_number)

    # 3) Linked TR-069 device by serial number
    stmt = (
        select(Tr069CpeDevice)
        .where(
            _normalized_serial_expr(Tr069CpeDevice.serial_number)
            == normalize_tr069_serial(ont.serial_number)
        )
        .where(Tr069CpeDevice.is_active.is_(True))
        .limit(1)
    )
    cpe = db.scalars(stmt).first()

    if cpe and cpe.acs_server_id:
        server = _resolve_server_by_id(db, str(cpe.acs_server_id))
        if server:
            client = GenieACSClient(server.base_url)
            try:
                device_id = _resolve_device_id_from_server(client, ont.serial_number)
            except GenieACSError:
                device_id = None
            if not device_id:
                device_id = client.build_device_id(
                    cpe.oui or "", cpe.product_class or "", cpe.serial_number or ""
                )
            return (client, device_id), "resolved_via_tr069_cpe_device"

    # 4) Default ACS server
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
                device_id = _resolve_device_id_from_server(client, ont.serial_number)
                if device_id:
                    return (client, device_id), "resolved_via_default_acs"
            except GenieACSError:
                logger.warning(
                    "Failed to search GenieACS for ONT %s", ont.serial_number
                )

    serial = str(getattr(ont, "serial_number", "") or "").strip()
    if not (olt_server or ont_server or default_server_id):
        return (
            None,
            "No ACS server configured on OLT, ONT, linked TR-069 device, or default settings.",
        )
    return None, f"No matching GenieACS device found for ONT serial '{serial}'."
