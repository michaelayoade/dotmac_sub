"""Network parameter actions for ONTs."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_ont_or_error,
    resolve_client_or_error,
)

logger = logging.getLogger(__name__)


def set_connection_request_credentials(
    db: Session,
    ont_id: str,
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

    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
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
            "Connection request credentials set on ONT %s (user: %s, root: %s)",
            ont.serial_number,
            username,
            root,
        )
        return ActionResult(
            success=True,
            message=f"Connection request credentials set on {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Set connection request credentials failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set connection request credentials: {exc}",
        )


def send_connection_request(db: Session, ont_id: str) -> ActionResult:
    """Send an HTTP connection request to the ONT for on-demand management.

    Reads the ConnectionRequestURL from the ACS device record
    and performs an HTTP GET with Digest auth.
    """
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)

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
            message="No ConnectionRequestURL found on device — ONT may not have bootstrapped yet.",
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

    import httpx

    try:
        with httpx.Client(timeout=10.0) as http:
            if conn_user:
                auth = httpx.DigestAuth(str(conn_user), str(conn_pass))
                resp = http.get(str(conn_url), auth=auth)
            else:
                resp = http.get(str(conn_url))
        if resp.status_code in (200, 204):
            logger.info(
                "Connection request sent to ONT %s at %s", ont.serial_number, conn_url
            )
            return ActionResult(
                success=True,
                message=f"Connection request sent to {ont.serial_number} ({resp.status_code}).",
            )
        logger.warning(
            "Connection request to ONT %s returned %d",
            ont.serial_number,
            resp.status_code,
        )
        return ActionResult(
            success=False,
            message=f"Connection request returned HTTP {resp.status_code}.",
        )
    except httpx.RequestError as exc:
        logger.error("Connection request failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Connection request failed: {exc}")


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    username: str,
    password: str,
    *,
    instance_index: int = 1,
) -> ActionResult:
    """Push PPPoE credentials to ONT via TR-069."""
    if not username:
        return ActionResult(success=False, message="PPPoE username is required.")
    if not password:
        return ActionResult(success=False, message="PPPoE password is required.")

    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    instance_index = max(1, instance_index)

    if root == "Device":
        params = {
            f"Device.PPP.Interface.{instance_index}.Username": username,
            f"Device.PPP.Interface.{instance_index}.Password": password,
        }
    else:
        params = {
            f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{instance_index}.WANPPPConnection.1.Username": username,
            f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{instance_index}.WANPPPConnection.1.Password": password,
        }
    try:
        result = client.set_parameter_values(device_id, params)
        ont.pppoe_username = username
        db.flush()
        logger.info(
            "PPPoE credentials set on ONT %s (user: %s, root: %s)",
            ont.serial_number,
            username,
            root,
        )
        return ActionResult(
            success=True,
            message=f"PPPoE credentials pushed to {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Set PPPoE credentials failed for ONT %s: %s", ont.serial_number, exc
        )
        return ActionResult(
            success=False, message=f"Failed to set PPPoE credentials: {exc}"
        )


def enable_ipv6_on_wan(
    db: Session,
    ont_id: str,
    *,
    wan_instance: int = 1,
) -> ActionResult:
    """Enable IPv6 dual-stack on the ONT WAN interface via TR-069.

    Configures DHCPv6 client for prefix delegation and enables IPv6 on the WAN.
    The ONT will request a /64 prefix from the NAS via DHCPv6-PD.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        wan_instance: WAN connection instance index (default 1).
    """
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)

    if root == "Device":
        params = {
            # Enable IPv6 on WAN interface
            f"Device.IP.Interface.{wan_instance}.IPv6Enable": "true",
            # Enable DHCPv6 client
            f"Device.DHCPv6.Client.{wan_instance}.Enable": "true",
            f"Device.DHCPv6.Client.{wan_instance}.RequestAddresses": "true",
            f"Device.DHCPv6.Client.{wan_instance}.RequestPrefixes": "true",
            # Enable Router Advertisement on LAN for SLAAC
            f"Device.RouterAdvertisement.InterfaceSettings.{wan_instance}.Enable": "true",
        }
    else:
        # InternetGatewayDevice (TR-098) — IPv6 support varies by firmware
        params = {
            f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{wan_instance}.WANIPConnection.1.X_IPv6Enabled": "1",
        }

    try:
        result = client.set_parameter_values(device_id, params)
        logger.info(
            "IPv6 dual-stack enabled on ONT %s (root: %s, instance: %d)",
            ont.serial_number,
            root,
            wan_instance,
        )
        return ActionResult(
            success=True,
            message=f"IPv6 dual-stack enabled on {ont.serial_number}. DHCPv6-PD will request a /64 prefix.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("IPv6 enable failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to enable IPv6: {exc}")
