"""Shared GenieACS device resolution for ONT services."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import settings_spec
from app.services.genieacs import GenieACSClient, GenieACSError

logger = logging.getLogger(__name__)


def _resolve_device_id_from_server(
    client: GenieACSClient,
    serial_number: str,
) -> str | None:
    devices = client.list_devices(query={"_id": {"$regex": f".*-{serial_number}$"}})
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
    """Resolve GenieACS client and device ID for an ONT.

    Looks up the TR-069 CPE device matching the ONT serial number,
    then builds the GenieACS device ID and client.

    Returns:
        Tuple of (client, device_id) or None if not resolvable.
    """
    # 1) OLT profile (authoritative in inherited ACS model)
    olt_server = None
    if ont.olt_device_id:
        olt = db.get(OLTDevice, str(ont.olt_device_id))
        if olt and olt.tr069_acs_server_id:
            olt_server = _resolve_server_by_id(db, str(olt.tr069_acs_server_id))
    if olt_server:
        client = GenieACSClient(olt_server.base_url)
        try:
            device_id = _resolve_device_id_from_server(client, ont.serial_number)
            if device_id:
                return client, device_id
        except GenieACSError:
            logger.warning("Failed OLT ACS lookup for ONT %s", ont.serial_number)

    # 2) Linked TR-069 device by serial number
    stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.serial_number == ont.serial_number)
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
            return client, device_id

    # 3) Default ACS server
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
                    return client, device_id
            except GenieACSError:
                logger.warning(
                    "Failed to search GenieACS for ONT %s", ont.serial_number
                )

    return None
