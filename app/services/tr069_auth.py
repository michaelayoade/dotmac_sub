"""TR-069 authentication credential service.

Provides device-specific credentials for:
- Connection Request (ACS -> CPE): Credentials ACS uses when triggering device inform
- CPE Authentication (CPE -> ACS): Credentials device uses when connecting to ACS
"""

import logging
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.models.tr069 import Tr069CpeDevice
from app.services.credential_crypto import decrypt_credential
from app.services.network.ont_desired_config import desired_config
from app.services.network.serial_utils import search_candidates

logger = logging.getLogger(__name__)


def get_device_credentials(
    db: Session,
    serial_number: str,
    credential_type: Literal["connection_request", "cpe_auth"],
) -> dict[str, str | None]:
    """Get credentials for a device by serial number.

    Looks up the device in both Tr069CpeDevice and OntUnit tables.
    Returns credentials from the effective ONT config if available.

    Args:
        db: Database session
        serial_number: Device serial number
        credential_type: Type of credentials to return

    Returns:
        Dict with username and password keys (may be None if not configured)
    """
    if not serial_number:
        return {"username": None, "password": None}

    # Normalize serial for matching
    candidates = search_candidates(serial_number)

    # Try to find TR-069 CPE device first
    cpe_device = None
    for candidate in candidates:
        cpe_stmt = select(Tr069CpeDevice).where(
            Tr069CpeDevice.serial_number.ilike(candidate)
        )
        cpe_device = db.scalars(cpe_stmt).first()
        if cpe_device:
            break

    # Try to find linked ONT unit
    ont_unit = None
    if cpe_device and cpe_device.ont_unit_id:
        ont_unit = db.get(OntUnit, cpe_device.ont_unit_id)
    elif not cpe_device:
        # Try direct ONT lookup by serial
        for candidate in candidates:
            ont_stmt = select(OntUnit).where(OntUnit.serial_number.ilike(candidate))
            ont_unit = db.scalars(ont_stmt).first()
            if ont_unit:
                break

    if credential_type == "connection_request":
        return _get_connection_request_credentials(ont_unit, cpe_device)
    else:
        return _get_cpe_auth_credentials(ont_unit, cpe_device)


def _get_connection_request_credentials(
    ont_unit: OntUnit | None,
    cpe_device: Tr069CpeDevice | None,
) -> dict[str, str | None]:
    """Get connection request credentials (ACS -> CPE).

    These are the credentials the ACS uses when sending connection requests
    to the CPE device to trigger an immediate inform.
    """
    username = None
    password = None

    # Check ONT desired config through the ownership helper.
    if ont_unit:
        config = desired_config(ont_unit)
        username = config.get("connection_request_username")
        encrypted_pass = config.get("connection_request_password")
        if encrypted_pass:
            try:
                password = decrypt_credential(encrypted_pass)
            except Exception:
                logger.warning(
                    "Failed to decrypt CR password for ONT %s",
                    ont_unit.id,
                    exc_info=True,
                )

    return {"username": username, "password": password}


def _get_cpe_auth_credentials(
    ont_unit: OntUnit | None,
    cpe_device: Tr069CpeDevice | None,
) -> dict[str, str | None]:
    """Get CPE authentication credentials (CPE -> ACS).

    These are the credentials the CPE device uses when connecting to the ACS.
    Stored in ManagementServer.Username/Password on the device.
    """
    username = None
    password = None

    # Check ONT desired config through the ownership helper.
    if ont_unit:
        config = desired_config(ont_unit)
        username = config.get("cwmp_username")
        encrypted_pass = config.get("cwmp_password")
        if encrypted_pass:
            try:
                password = decrypt_credential(encrypted_pass)
            except Exception:
                logger.warning(
                    "Failed to decrypt CWMP password for ONT %s",
                    ont_unit.id,
                    exc_info=True,
                )

    return {"username": username, "password": password}
