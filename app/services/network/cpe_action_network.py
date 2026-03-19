"""Network parameter actions for CPE devices."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_cpe_or_error,
    resolve_cpe_client_or_error,
)

logger = logging.getLogger(__name__)


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
        return ActionResult(success=False, message="Connection request username is required.")
    if not password:
        return ActionResult(success=False, message="Connection request password is required.")

    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, cpe, client, device_id)
    params = build_tr069_params(root, {
        "ManagementServer.ConnectionRequestUsername": username,
        "ManagementServer.ConnectionRequestPassword": password,
        "ManagementServer.PeriodicInformInterval": periodic_inform_interval,
    })
    try:
        result = client.set_parameter_values(device_id, params)
        logger.info(
            "Connection request credentials set on CPE %s (user: %s, root: %s)",
            cpe.serial_number, username, root,
        )
        return ActionResult(
            success=True,
            message=f"Connection request credentials set on {cpe.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Set connection request credentials failed for CPE %s: %s",
            cpe.serial_number, exc,
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
    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
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

    conn_user = client.extract_parameter_value(
        device, f"{root}.ManagementServer.ConnectionRequestUsername"
    ) or ""
    conn_pass = client.extract_parameter_value(
        device, f"{root}.ManagementServer.ConnectionRequestPassword"
    ) or ""

    import httpx

    try:
        with httpx.Client(timeout=10.0) as http:
            if conn_user:
                auth = httpx.DigestAuth(str(conn_user), str(conn_pass))
                resp = http.get(str(conn_url), auth=auth)
            else:
                resp = http.get(str(conn_url))
        if resp.status_code in (200, 204):
            logger.info("Connection request sent to CPE %s at %s", cpe.serial_number, conn_url)
            return ActionResult(
                success=True,
                message=f"Connection request sent to {cpe.serial_number} ({resp.status_code}).",
            )
        logger.warning(
            "Connection request to CPE %s returned %d", cpe.serial_number, resp.status_code
        )
        return ActionResult(
            success=False,
            message=f"Connection request returned HTTP {resp.status_code}.",
        )
    except httpx.RequestError as exc:
        logger.error("Connection request failed for CPE %s: %s", cpe.serial_number, exc)
        return ActionResult(success=False, message=f"Connection request failed: {exc}")
