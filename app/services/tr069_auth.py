"""TR-069 authentication credential service.

Provides device-specific credentials for:
- Connection Request (ACS -> CPE): Credentials ACS uses when triggering device inform
- CPE Authentication (CPE -> ACS): Credentials device uses when connecting to ACS
"""

import logging
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAuthorizationStatus, OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.credential_crypto import decrypt_credential
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.serial_utils import search_candidates

logger = logging.getLogger(__name__)


def get_device_credentials(
    db: Session,
    serial_number: str,
    credential_type: Literal["connection_request", "cpe_auth"],
) -> dict[str, str | bool | None]:
    """Get credentials for a device by serial number.

    Looks up the device in both Tr069CpeDevice and OntUnit tables.
    Returns credentials from the ONT's bound ACS server. The ACS server row is
    the only credential source of truth for GenieACS authentication.

    Args:
        db: Database session
        serial_number: Device serial number
        credential_type: Type of credentials to return

    Returns:
        Dict with username and password keys (may be None if not configured)
    """
    if not serial_number:
        return {"username": None, "password": None, "authorized": False}

    candidates = search_candidates(serial_number)
    ont_unit = _find_authorized_ont(db, candidates)
    server = _resolve_bound_acs_server(db, ont_unit)

    username: str | None
    if credential_type == "connection_request":
        username = str(getattr(server, "connection_request_username", "") or "").strip()
        encrypted_password = (
            getattr(server, "connection_request_password", None) if server else None
        )
        label = "connection request"
    else:
        username = str(getattr(server, "cwmp_username", "") or "").strip()
        encrypted_password = getattr(server, "cwmp_password", None) if server else None
        label = "CWMP"

    password = _decrypt_server_password(encrypted_password, ont_unit, label)
    if not username or not password:
        username = None
        password = None

    return {
        "username": username,
        "password": password,
        "authorized": bool(ont_unit),
    }


def _find_authorized_ont(db: Session, candidates: list[str]) -> OntUnit | None:
    for candidate in candidates:
        cpe_stmt = (
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.serial_number.ilike(candidate))
            .where(Tr069CpeDevice.is_active.is_(True))
        )
        cpe_device = db.scalars(cpe_stmt).first()
        if cpe_device and cpe_device.ont_unit_id:
            ont_unit = db.get(OntUnit, cpe_device.ont_unit_id)
            if _is_authorized_ont(ont_unit):
                return ont_unit

    # Direct lookup handles stale inactive TR-069 rows that still carry the
    # plain HWTC serial with no ONT link.
    for candidate in candidates:
        ont_stmt = (
            select(OntUnit)
            .where(OntUnit.serial_number.ilike(candidate))
            .where(OntUnit.is_active.is_(True))
            .where(OntUnit.authorization_status == OntAuthorizationStatus.authorized)
        )
        ont_unit = db.scalars(ont_stmt).first()
        if ont_unit:
            return ont_unit

    return None


def _is_authorized_ont(ont_unit: OntUnit | None) -> bool:
    if ont_unit is None:
        return False
    return bool(
        getattr(ont_unit, "is_active", False)
        and getattr(ont_unit, "authorization_status", None)
        == OntAuthorizationStatus.authorized
    )


def _resolve_bound_acs_server(
    db: Session,
    ont_unit: OntUnit | None,
) -> Tr069AcsServer | None:
    if ont_unit is None:
        return None

    server = None
    try:
        effective = resolve_effective_ont_config(db, ont_unit)
        values = effective.get("values", {})
        acs_server_id = values.get("tr069_acs_server_id")
        if acs_server_id:
            server = db.get(Tr069AcsServer, acs_server_id)
    except Exception:
        logger.warning(
            "Failed to resolve bound ACS server for ONT %s",
            ont_unit.id,
            exc_info=True,
        )

    if server is not None and getattr(server, "is_active", False):
        return server
    return None


def _decrypt_server_password(
    encrypted_password: str | None,
    ont_unit: OntUnit | None,
    label: str,
) -> str | None:
    if not encrypted_password:
        return None
    try:
        return decrypt_credential(encrypted_password)
    except Exception:
        logger.warning(
            "Failed to decrypt %s password for ONT %s",
            label,
            getattr(ont_unit, "id", None),
            exc_info=True,
        )
        return None
