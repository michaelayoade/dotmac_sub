"""Shared GenieACS device resolution for ONT services."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import settings_spec
from app.services.genieacs import GenieACSClient, GenieACSError

logger = logging.getLogger(__name__)


def resolve_genieacs(
    db: Session, ont: OntUnit
) -> tuple[GenieACSClient, str] | None:
    """Resolve GenieACS client and device ID for an ONT.

    Looks up the TR-069 CPE device matching the ONT serial number,
    then builds the GenieACS device ID and client.

    Returns:
        Tuple of (client, device_id) or None if not resolvable.
    """
    # Find CPE device by serial number
    stmt = (
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.serial_number == ont.serial_number)
        .where(Tr069CpeDevice.is_active.is_(True))
        .limit(1)
    )
    cpe = db.scalars(stmt).first()

    if cpe and cpe.acs_server_id:
        server = db.get(Tr069AcsServer, str(cpe.acs_server_id))
        if server and server.base_url:
            client = GenieACSClient(server.base_url)
            device_id = client.build_device_id(
                cpe.oui or "", cpe.product_class or "", cpe.serial_number or ""
            )
            return client, device_id

    # Fallback: try default ACS server with serial number query
    default_server_id = settings_spec.resolve_value(
        db,
        SettingDomain.tr069,
        "default_acs_server_id",
    )
    if default_server_id:
        server = db.get(Tr069AcsServer, str(default_server_id))
        if server and server.base_url:
            client = GenieACSClient(server.base_url)
            # Search by serial number in GenieACS
            try:
                devices = client.list_devices(
                    query={"_id": {"$regex": f".*-{ont.serial_number}$"}}
                )
                if devices:
                    device_id = devices[0].get("_id", "")
                    if device_id:
                        return client, device_id
            except GenieACSError:
                logger.warning(
                    "Failed to search GenieACS for ONT %s", ont.serial_number
                )

    return None
