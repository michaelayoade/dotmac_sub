"""Network parameter actions for CPE devices."""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.credential_crypto import decrypt_credential
from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_cpe_client_or_error,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def _send_connection_request_http(
    conn_url: str,
    username: str | None = None,
    password: str | None = None,
) -> int:
    with httpx.Client(timeout=10.0) as http:
        if username:
            response = http.get(
                str(conn_url),
                auth=httpx.DigestAuth(str(username), str(password)),
            )
        else:
            response = http.get(str(conn_url))
    return response.status_code


def _resolve_cpe_fallback_connection_request_auth(
    db: Session,
    cpe_id: str,
) -> tuple[str, str] | None:
    linked = db.scalars(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.cpe_device_id == cpe_id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .order_by(Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc())
        .limit(1)
    ).first()
    server = (
        db.get(Tr069AcsServer, linked.acs_server_id)
        if linked and linked.acs_server_id
        else None
    )
    if server is None:
        default_server_id = resolve_value(
            db, SettingDomain.tr069, "default_acs_server_id"
        )
        if default_server_id:
            server = db.get(Tr069AcsServer, str(default_server_id))
    if not server:
        return None
    username = str(server.connection_request_username or "").strip()
    password = decrypt_credential(server.connection_request_password) or ""
    if not username and not password:
        return None
    return username, password


def set_connection_request_credentials(
    db: Session,
    cpe_id: str,
    username: str,
    password: str,
    *,
    periodic_inform_interval: int = 300,
) -> ActionResult:
    """Set TR-069 Connection Request credentials and periodic inform interval."""
    if not username:
        return ActionResult(
            success=False, message="Connection request username is required."
        )
    if not password:
        return ActionResult(
            success=False, message="Connection request password is required."
        )

    resolved, error = get_cpe_client_or_error(db, cpe_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="CPE device resolution failed.")
    cpe, client, device_id = resolved
    root = detect_data_model_root(db, cpe, client, device_id)
    params = build_tr069_params(
        root,
        {
            "ManagementServer.ConnectionRequestUsername": username,
            "ManagementServer.ConnectionRequestPassword": password,
            "ManagementServer.PeriodicInformInterval": periodic_inform_interval,
        },
    )
    try:
        result = client.set_parameter_values(device_id, params)
        logger.info(
            "Connection request credentials set on CPE %s (user: %s, root: %s)",
            cpe.serial_number,
            username,
            root,
        )
        return ActionResult(
            success=True,
            message=f"Connection request credentials set on {cpe.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Set connection request credentials failed for CPE %s: %s",
            cpe.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set connection request credentials: {exc}",
        )


def send_connection_request(db: Session, cpe_id: str) -> ActionResult:
    """Send an HTTP connection request to the CPE for on-demand management.

    Reads the ConnectionRequestURL from the ACS device record
    and performs an HTTP GET with Digest auth.
    """
    resolved, error = get_cpe_client_or_error(db, cpe_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="CPE device resolution failed.")
    cpe, client, device_id = resolved
    root = detect_data_model_root(db, cpe, client, device_id)

    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(success=False, message=f"Failed to fetch device: {exc}")

    conn_url = client.extract_parameter_value(
        device, f"{root}.ManagementServer.ConnectionRequestURL"
    )
    if not conn_url:
        return ActionResult(
            success=False,
            message="No ConnectionRequestURL found — CPE may not have bootstrapped yet.",
        )

    conn_user = (
        client.extract_parameter_value(
            device, f"{root}.ManagementServer.ConnectionRequestUsername"
        )
        or ""
    )
    conn_pass = (
        client.extract_parameter_value(
            device, f"{root}.ManagementServer.ConnectionRequestPassword"
        )
        or ""
    )

    try:
        status_code = _send_connection_request_http(
            str(conn_url),
            str(conn_user),
            str(conn_pass),
        )
        if status_code == 401:
            fallback_auth = _resolve_cpe_fallback_connection_request_auth(
                db, str(cpe.id)
            )
            if fallback_auth:
                fallback_user, fallback_pass = fallback_auth
                if (fallback_user, fallback_pass) != (str(conn_user), str(conn_pass)):
                    status_code = _send_connection_request_http(
                        str(conn_url),
                        fallback_user,
                        fallback_pass,
                    )
        if status_code in (200, 204):
            logger.info(
                "Connection request sent to CPE %s at %s", cpe.serial_number, conn_url
            )
            return ActionResult(
                success=True,
                message=f"Connection request sent to {cpe.serial_number} ({status_code}).",
            )
        logger.warning(
            "Connection request to CPE %s returned %d",
            cpe.serial_number,
            status_code,
        )
        return ActionResult(
            success=False,
            message=f"Connection request returned HTTP {status_code}.",
        )
    except httpx.RequestError as exc:
        logger.error("Connection request failed for CPE %s: %s", cpe.serial_number, exc)
        return ActionResult(success=False, message=f"Connection request failed: {exc}")
