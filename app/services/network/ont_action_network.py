"""Network parameter actions for ONTs."""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import CPEDevice
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.credential_crypto import decrypt_credential
from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_ont_client_or_error,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def _normalized_serial_expr(column):  # type: ignore[no-untyped-def]
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _normalize_serial(value: str | None) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


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


def _resolve_ont_fallback_connection_request_auth(
    db: Session,
    serial_number: str | None,
) -> tuple[str, str] | None:
    server: Tr069AcsServer | None = None

    cpe = None
    serial = str(serial_number or "").strip()
    if serial:
        normalized_serial = _normalize_serial(serial)
        cpe = db.scalars(
            select(CPEDevice)
            .where(
                _normalized_serial_expr(CPEDevice.serial_number) == normalized_serial
            )
            .order_by(CPEDevice.updated_at.desc(), CPEDevice.created_at.desc())
            .limit(1)
        ).first()
    linked = None
    if cpe is not None:
        linked = db.scalars(
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.cpe_device_id == cpe.id)
            .where(Tr069CpeDevice.is_active.is_(True))
            .order_by(
                Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc()
            )
            .limit(1)
        ).first()
    if linked is None and serial:
        normalized_serial = _normalize_serial(serial)
        linked = db.scalars(
            select(Tr069CpeDevice)
            .where(
                _normalized_serial_expr(Tr069CpeDevice.serial_number)
                == normalized_serial
            )
            .where(Tr069CpeDevice.is_active.is_(True))
            .order_by(
                Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc()
            )
            .limit(1)
        ).first()
    if linked and linked.acs_server_id:
        server = db.get(Tr069AcsServer, linked.acs_server_id)
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

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
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
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
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

    try:
        status_code = _send_connection_request_http(
            str(conn_url),
            str(conn_user),
            str(conn_pass),
        )
        if status_code == 401:
            fallback_auth = _resolve_ont_fallback_connection_request_auth(
                db, ont.serial_number
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
                "Connection request sent to ONT %s at %s", ont.serial_number, conn_url
            )
            return ActionResult(
                success=True,
                message=f"Connection request sent to {ont.serial_number} ({status_code}).",
            )
        logger.warning(
            "Connection request to ONT %s returned %d",
            ont.serial_number,
            status_code,
        )
        return ActionResult(
            success=False,
            message=f"Connection request returned HTTP {status_code}.",
        )
    except httpx.RequestError as exc:
        logger.error("Connection request failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Connection request failed: {exc}")


def send_connection_request_tracked(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
) -> ActionResult:
    """Tracked wrapper around send_connection_request.

    Creates a NetworkOperation record to track the action lifecycle,
    then delegates to the existing function.
    """
    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    try:
        op = network_operations.start(
            db,
            NetworkOperationType.ont_send_conn_request,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=f"ont_conn_req:{ont_id}",
            initiated_by=initiated_by,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return ActionResult(
                success=False,
                message="A connection request is already in progress for this ONT.",
            )
        raise
    network_operations.mark_running(db, str(op.id))
    db.flush()

    try:
        result = send_connection_request(db, ont_id)
        try:
            if result.success:
                network_operations.mark_succeeded(
                    db, str(op.id), output_payload=result.data
                )
            else:
                network_operations.mark_failed(db, str(op.id), result.message)
        except Exception as track_err:
            logger.error("Failed to record operation outcome for %s: %s", op.id, track_err)
        return result
    except Exception as exc:
        try:
            network_operations.mark_failed(db, str(op.id), str(exc))
        except Exception as track_err:
            logger.error(
                "Failed to record operation failure for %s: %s (original: %s)",
                op.id, track_err, exc,
            )
            db.rollback()
        raise


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

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
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
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
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
