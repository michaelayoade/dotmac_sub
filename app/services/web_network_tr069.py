"""Service helpers for admin TR-069 web routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.network import CPEDevice, OntAssignment, OntUnit
from app.models.tr069 import Tr069CpeDevice, Tr069Job, Tr069JobStatus
from app.schemas.network import OntUnitCreate
from app.schemas.tr069 import (
    Tr069AcsServerCreate,
    Tr069AcsServerUpdate,
    Tr069CpeDeviceUpdate,
    Tr069JobCreate,
)
from app.services import network as network_service
from app.services.network import cpe as cpe_service
from app.services import tr069 as tr069_service
from app.services.common import coerce_uuid
from app.services.genieacs import GenieACSClient, GenieACSError, normalize_tr069_serial
from app.services.network._common import decode_huawei_hex_serial

logger = logging.getLogger(__name__)


def _normalized_serial_expr(column):  # type: ignore[no-untyped-def]
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


@dataclass
class JobAction:
    label: str
    command: str
    payload: dict | None = None


_JOB_ACTIONS: dict[str, JobAction] = {
    "refresh": JobAction(
        label="Refresh Parameters",
        command="refreshObject",
        payload={"objectName": "Device."},
    ),
    "reboot": JobAction(label="Reboot Device", command="reboot"),
    "factory_reset": JobAction(label="Factory Reset", command="factoryReset"),
}


# Config push actions use setParameterValues - need parameter values at runtime
@dataclass
class ConfigAction:
    """Defines a config push action with its TR-069 parameter mappings."""

    label: str
    description: str
    parameters: list[str]  # List of parameter paths that can be set


_CONFIG_ACTIONS: dict[str, ConfigAction] = {
    "wifi_ssid": ConfigAction(
        label="Set WiFi SSID",
        description="Change the WiFi network name (SSID)",
        parameters=[
            "Device.WiFi.SSID.1.SSID",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
        ],
    ),
    "wifi_password": ConfigAction(
        label="Set WiFi Password",
        description="Change the WiFi password (WPA key)",
        parameters=[
            "Device.WiFi.AccessPoint.1.Security.KeyPassphrase",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase",
        ],
    ),
    "wifi_enable": ConfigAction(
        label="Enable WiFi",
        description="Turn on the WiFi radio",
        parameters=[
            "Device.WiFi.Radio.1.Enable",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
        ],
    ),
    "wifi_disable": ConfigAction(
        label="Disable WiFi",
        description="Turn off the WiFi radio",
        parameters=[
            "Device.WiFi.Radio.1.Enable",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
        ],
    ),
    "pppoe_username": ConfigAction(
        label="Set PPPoE Username",
        description="Change the PPPoE username for WAN connection",
        parameters=[
            "Device.PPP.Interface.1.Username",
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
        ],
    ),
    "pppoe_password": ConfigAction(
        label="Set PPPoE Password",
        description="Change the PPPoE password for WAN connection",
        parameters=[
            "Device.PPP.Interface.1.Password",
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        ],
    ),
    "lan_ip": ConfigAction(
        label="Set LAN IP Address",
        description="Change the LAN gateway IP address",
        parameters=[
            "Device.IP.Interface.2.IPv4Address.1.IPAddress",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
        ],
    ),
    "lan_subnet": ConfigAction(
        label="Set LAN Subnet Mask",
        description="Change the LAN subnet mask",
        parameters=[
            "Device.IP.Interface.2.IPv4Address.1.SubnetMask",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
        ],
    ),
    "dhcp_enable": ConfigAction(
        label="Enable DHCP Server",
        description="Turn on the DHCP server for LAN clients",
        parameters=[
            "Device.DHCPv4.Server.Enable",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
        ],
    ),
    "dhcp_disable": ConfigAction(
        label="Disable DHCP Server",
        description="Turn off the DHCP server",
        parameters=[
            "Device.DHCPv4.Server.Enable",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
        ],
    ),
}


def parse_acs_form(form) -> dict[str, object]:
    # Parse periodic inform interval with validation
    interval_str = str(form.get("periodic_inform_interval") or "300").strip()
    try:
        periodic_inform_interval = max(60, min(86400, int(interval_str)))
    except ValueError:
        periodic_inform_interval = 300

    return {
        "name": str(form.get("name") or "").strip(),
        "cwmp_url": str(form.get("cwmp_url") or "").strip(),
        "cwmp_username": str(form.get("cwmp_username") or "").strip(),
        "cwmp_password": str(form.get("cwmp_password") or "").strip(),
        "connection_request_username": str(
            form.get("connection_request_username") or ""
        ).strip(),
        "connection_request_password": str(
            form.get("connection_request_password") or ""
        ).strip(),
        "base_url": str(form.get("base_url") or "").strip(),
        "periodic_inform_interval": periodic_inform_interval,
        "is_active": str(form.get("is_active") or "true").strip().lower()
        in ("1", "true", "on", "yes"),
        "notes": str(form.get("notes") or "").strip() or None,
    }


def validate_acs_values(values: dict[str, object]) -> str | None:
    if not values.get("name"):
        return "ACS server name is required."
    if not values.get("cwmp_url"):
        return "CWMP URL is required."
    if not values.get("cwmp_username"):
        return "CWMP username is required."
    if not values.get("cwmp_password"):
        return "CWMP password is required."
    if not values.get("connection_request_username"):
        return "Connection request username is required."
    if not values.get("connection_request_password"):
        return "Connection request password is required."
    if not values.get("base_url"):
        return "ACS base URL is required."
    return None


def validate_acs_connection(values: dict[str, object]) -> str | None:
    base_url = str(values.get("base_url") or "").strip()
    if not base_url:
        return "ACS base URL is required."

    try:
        # Lightweight reachability/auth check against GenieACS NBI.
        GenieACSClient(base_url, timeout=5.0).count_devices()
    except GenieACSError as exc:
        return f"Failed to connect to GenieACS ({base_url}): {exc}"
    return None


def acs_form_snapshot(
    values: dict[str, object], *, acs_id: str | None = None
) -> dict[str, object]:
    data = dict(values)
    if acs_id:
        data["id"] = acs_id
    return data


def acs_form_snapshot_from_model(server) -> dict[str, object]:
    return {
        "id": str(server.id),
        "name": server.name,
        "cwmp_url": server.cwmp_url,
        "cwmp_username": server.cwmp_username,
        "cwmp_password": "",  # nosec
        "connection_request_username": server.connection_request_username,
        "connection_request_password": "",  # nosec
        "base_url": server.base_url,
        "periodic_inform_interval": getattr(server, "periodic_inform_interval", 300),
        "is_active": bool(server.is_active),
        "notes": server.notes or "",
    }


def create_acs_server(db: Session, values: dict[str, object]):
    connection_error = validate_acs_connection(values)
    if connection_error:
        raise ValueError(connection_error)
    payload = Tr069AcsServerCreate.model_validate(values)
    return tr069_service.acs_servers.create(db=db, payload=payload)


def update_acs_server(db: Session, *, acs_id: str, values: dict[str, object]):
    existing = get_acs_server(db, acs_id=acs_id)
    if not str(values.get("cwmp_password") or "").strip():
        values["cwmp_password"] = existing.cwmp_password
    if not str(values.get("connection_request_password") or "").strip():
        values["connection_request_password"] = existing.connection_request_password
    connection_error = validate_acs_connection(values)
    if connection_error:
        raise ValueError(connection_error)
    payload = Tr069AcsServerUpdate.model_validate(values)
    return tr069_service.acs_servers.update(db=db, server_id=acs_id, payload=payload)


def get_acs_server(db: Session, *, acs_id: str):
    return tr069_service.acs_servers.get(db=db, server_id=acs_id)


def queue_device_job(db: Session, *, tr069_device_id: str, action: str):
    selected = _JOB_ACTIONS.get(action)
    if selected is None:
        raise ValueError("Unsupported TR-069 action.")

    payload = Tr069JobCreate(
        device_id=coerce_uuid(tr069_device_id),
        name=selected.label,
        command=selected.command,
        payload=selected.payload,
    )
    job = tr069_service.jobs.create(db=db, payload=payload)
    return tr069_service.jobs.execute(db=db, job_id=str(job.id))


def link_tr069_device_to_cpe(
    db: Session,
    *,
    tr069_device_id: str,
    cpe_device_id: str | None,
):
    payload = Tr069CpeDeviceUpdate(
        cpe_device_id=coerce_uuid(cpe_device_id) if cpe_device_id else None,
    )
    return tr069_service.cpe_devices.update(
        db=db,
        device_id=tr069_device_id,
        payload=payload,
    )


def _display_serial_number(value: str | None) -> str | None:
    serial = str(value or "").strip()
    if not serial:
        return None
    return decode_huawei_hex_serial(serial) or serial


def tr069_dashboard_data(
    db: Session,
    *,
    acs_server_id: str | None = None,
    search: str | None = None,
    only_unlinked: bool = False,
) -> dict[str, object]:
    inventory_subscriber_id = cpe_service.get_inventory_subscriber_id(db)

    def _cpe_primary_label(cpe: CPEDevice) -> str:
        if (
            cpe.subscriber
            and getattr(cpe.subscriber, "full_name", None)
            and getattr(cpe, "subscriber_id", None) != inventory_subscriber_id
        ):
            return str(cpe.subscriber.full_name)
        if cpe.serial_number:
            return str(cpe.serial_number)
        if cpe.model:
            return str(cpe.model)
        if cpe.mac_address:
            return str(cpe.mac_address)
        return f"CPE {str(cpe.id)[:8]}"

    def _cpe_search_label(cpe: CPEDevice) -> str:
        parts = [_cpe_primary_label(cpe)]
        if cpe.serial_number:
            parts.append(f"SN:{cpe.serial_number}")
        if cpe.model:
            parts.append(f"Model:{cpe.model}")
        if cpe.mac_address:
            parts.append(f"MAC:{cpe.mac_address}")
        parts.append(f"[{str(cpe.id)[:8]}]")
        return " | ".join(parts)

    servers = tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    selected_server_id = str(acs_server_id or "").strip() or None
    if not selected_server_id and servers:
        selected_server_id = str(servers[0].id)

    devices = (
        tr069_service.cpe_devices.list(
            db=db,
            acs_server_id=selected_server_id,
            is_active=None,
            order_by="serial_number",
            order_dir="asc",
            limit=5000,
            offset=0,
        )
        if selected_server_id
        else []
    )

    search_q = str(search or "").strip().lower()
    if search_q:
        devices = [
            item
            for item in devices
            if search_q
            in " ".join(
                [
                    str(item.serial_number or ""),
                    str(item.oui or ""),
                    str(item.product_class or ""),
                    str(item.connection_request_url or ""),
                ]
            ).lower()
        ]

    if only_unlinked:
        devices = [item for item in devices if not item.cpe_device_id]

    linked_cpe_ids = [item.cpe_device_id for item in devices if item.cpe_device_id]
    linked_cpes = (
        db.query(CPEDevice)
        .options(joinedload(CPEDevice.subscriber))
        .filter(CPEDevice.id.in_(linked_cpe_ids))
        .all()
        if linked_cpe_ids
        else []
    )
    cpe_by_id = {str(cpe.id): cpe for cpe in linked_cpes}
    normalized_serials = {
        normalize_tr069_serial(
            _display_serial_number(item.serial_number) or item.serial_number or ""
        )
        for item in devices
        if item.serial_number
    }
    ont_by_normalized_serial: dict[str, OntUnit] = {}
    if normalized_serials:
        onts = list(
            db.scalars(
                select(OntUnit).options(
                    joinedload(OntUnit.olt_device),
                    joinedload(OntUnit.assignments).joinedload(OntAssignment.pon_port),
                )
            )
            .unique()
            .all()
        )
        ont_by_normalized_serial = {
            serial: ont
            for ont in onts
            for serial in [normalize_tr069_serial(ont.serial_number or "")]
            if serial in normalized_serials
        }

    for device in devices:
        device.linked_cpe = (
            cpe_by_id.get(str(device.cpe_device_id)) if device.cpe_device_id else None
        )
        device.linked_cpe_display_label = (
            _cpe_primary_label(device.linked_cpe) if device.linked_cpe else None
        )
        device.display_serial_number = _display_serial_number(device.serial_number)
        normalized_serial = normalize_tr069_serial(
            device.display_serial_number or device.serial_number or ""
        )
        device.linked_ont = ont_by_normalized_serial.get(normalized_serial)
        active_assignment = next(
            (
                assignment
                for assignment in getattr(device.linked_ont, "assignments", [])
                if getattr(assignment, "active", False)
            ),
            None,
        )
        device.linked_pon_port_name = (
            getattr(getattr(active_assignment, "pon_port", None), "name", None) or None
        )
        device.linked_olt_name = (
            getattr(getattr(device.linked_ont, "olt_device", None), "name", None)
            or None
        )

    unconfigured_devices = [item for item in devices if not item.cpe_device_id]
    configured_devices = [item for item in devices if item.cpe_device_id]

    jobs = tr069_service.jobs.list(
        db=db,
        device_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )

    managed_cpes = (
        db.query(CPEDevice)
        .options(joinedload(CPEDevice.subscriber))
        .order_by(CPEDevice.created_at.desc())
        .limit(1000)
        .all()
    )
    if inventory_subscriber_id is not None:
        managed_cpes = [
            cpe
            for cpe in managed_cpes
            if getattr(cpe, "subscriber_id", None) != inventory_subscriber_id
        ]
    cpe_typeahead_map = {_cpe_search_label(cpe): str(cpe.id) for cpe in managed_cpes}
    cpe_display_by_id = {str(cpe.id): _cpe_primary_label(cpe) for cpe in managed_cpes}
    cpe_search_by_id = {str(cpe.id): _cpe_search_label(cpe) for cpe in managed_cpes}

    now = datetime.now(UTC)
    seen_window = now - timedelta(hours=24)

    def _seen_recently(item: Tr069CpeDevice) -> bool:
        informed_at = item.last_inform_at
        if informed_at is None:
            return False
        if informed_at.tzinfo is None:
            informed_at = informed_at.replace(tzinfo=UTC)
        return informed_at >= seen_window

    return {
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "devices": devices,
        "configured_devices": configured_devices,
        "unconfigured_devices": unconfigured_devices,
        "recent_jobs": jobs,
        "managed_cpes": managed_cpes,
        "job_actions": _JOB_ACTIONS,
        "config_actions": _CONFIG_ACTIONS,
        "cpe_typeahead_map": cpe_typeahead_map,
        "cpe_typeahead_labels": list(cpe_typeahead_map.keys()),
        "cpe_display_by_id": cpe_display_by_id,
        "cpe_search_by_id": cpe_search_by_id,
        "stats": {
            "servers": len(servers),
            "devices": len(devices),
            "unlinked": len(unconfigured_devices),
            "configured": len(configured_devices),
            "unconfigured": len(unconfigured_devices),
            "seen_24h": sum(1 for item in devices if _seen_recently(item)),
            "jobs_failed": sum(
                1 for item in jobs if item.status == Tr069JobStatus.failed
            ),
        },
        "filters": {
            "search": str(search or "").strip(),
            "only_unlinked": bool(only_unlinked),
        },
    }


def sync_server(db: Session, *, acs_server_id: str) -> dict[str, int]:
    return tr069_service.cpe_devices.sync_from_genieacs(
        db=db, acs_server_id=acs_server_id
    )


def create_ont_from_tr069_device(
    db: Session, *, tr069_device_id: str
) -> tuple[OntUnit, bool]:
    """Create or resolve an ONT inventory record from a synced TR-069 device.

    Returns (ont, created_new).
    """
    device = db.get(Tr069CpeDevice, coerce_uuid(tr069_device_id))
    if not device:
        raise ValueError("TR-069 device not found")

    display_serial = (
        _display_serial_number(device.serial_number)
        or str(device.serial_number or "").strip()
    )
    if not display_serial:
        raise ValueError("TR-069 device has no usable serial number")

    normalized_serial = normalize_tr069_serial(display_serial)
    existing = (
        db.query(OntUnit)
        .filter(_normalized_serial_expr(OntUnit.serial_number) == normalized_serial)
        .first()
    )
    if existing:
        tr069_service.link_tr069_device_to_ont(
            db,
            device,
            existing,
            acs_server_id=device.acs_server_id,
        )
        db.commit()
        db.refresh(existing)
        return existing, False

    payload = OntUnitCreate(
        serial_number=display_serial,
        vendor="Huawei"
        if str(device.oui or "").upper().startswith(("48575443", "HWTC", "HWTT"))
        else None,
        model=device.product_class or None,
        is_active=False,
        name=display_serial,
        notes=f"Imported from TR-069 device {device.id}",
    )
    ont = network_service.ont_units.create(db=db, payload=payload)
    tr069_service.link_tr069_device_to_ont(
        db,
        device,
        ont,
        acs_server_id=device.acs_server_id,
    )
    db.commit()
    db.refresh(ont)
    return ont, True


def get_config_actions() -> dict[str, ConfigAction]:
    """Return available config push actions."""
    return _CONFIG_ACTIONS


def create_config_push_job(
    db: Session,
    *,
    tr069_device_id: str,
    action_key: str,
    value: str,
) -> Tr069Job:
    """Create a config push job (setParameterValues) for a TR-069 device.

    Args:
        db: Database session
        tr069_device_id: TR-069 CPE device ID
        action_key: Config action key (e.g., 'wifi_ssid', 'pppoe_username')
        value: Value to set

    Returns:
        Created job
    """
    config_action = _CONFIG_ACTIONS.get(action_key)
    if config_action is None:
        raise ValueError(f"Unknown config action: {action_key}")

    device = db.get(Tr069CpeDevice, coerce_uuid(tr069_device_id))
    if not device:
        raise ValueError("TR-069 device not found")

    # Build parameter values - try TR-181 path first, fallback to TR-098
    # Device. paths are TR-181, InternetGatewayDevice. paths are TR-098
    parameter_path = config_action.parameters[0]  # Primary path
    parameter_value = value

    # Handle boolean toggle actions
    if action_key == "wifi_enable":
        parameter_value = "true"
    elif action_key == "wifi_disable":
        parameter_value = "false"
    elif action_key == "dhcp_enable":
        parameter_value = "true"
    elif action_key == "dhcp_disable":
        parameter_value = "false"

    # Create job with setParameterValues command
    payload = Tr069JobCreate(
        device_id=coerce_uuid(tr069_device_id),
        name=config_action.label,
        command="setParameterValues",
        payload={
            "parameterValues": [[parameter_path, parameter_value, "xsd:string"]],
        },
    )
    job = tr069_service.jobs.create(db=db, payload=payload)

    # Execute immediately
    return tr069_service.jobs.execute(db=db, job_id=str(job.id))


def create_firmware_download_job(
    db: Session,
    *,
    tr069_device_id: str,
    firmware_url: str,
    filename: str | None = None,
) -> Tr069Job:
    """Create a firmware download job for a TR-069 device.

    Args:
        db: Database session
        tr069_device_id: TR-069 CPE device ID
        firmware_url: URL to download firmware from
        filename: Optional filename for the firmware

    Returns:
        Created job
    """
    device = db.get(Tr069CpeDevice, coerce_uuid(tr069_device_id))
    if not device:
        raise ValueError("TR-069 device not found")

    if not firmware_url or not firmware_url.strip():
        raise ValueError("Firmware URL is required")

    # Build download task payload
    task_payload: dict[str, object] = {
        "fileType": "1 Firmware Upgrade Image",
        "url": firmware_url.strip(),
    }
    if filename and filename.strip():
        task_payload["filename"] = filename.strip()

    payload = Tr069JobCreate(
        device_id=coerce_uuid(tr069_device_id),
        name="Firmware Update",
        command="download",
        payload=task_payload,
    )
    job = tr069_service.jobs.create(db=db, payload=payload)

    # Execute immediately
    return tr069_service.jobs.execute(db=db, job_id=str(job.id))


def queue_bulk_action(
    device_ids: list[str],
    action: str,
    params: dict | None = None,
) -> str:
    """Queue a bulk action for multiple TR-069 devices.

    Args:
        device_ids: List of TR-069 device UUIDs
        action: Action to execute (refresh, reboot, factory_reset, config_push, firmware)
        params: Additional parameters for certain actions

    Returns:
        Celery task ID
    """
    if not device_ids:
        raise ValueError("No devices selected for bulk action")

    valid_actions = {"refresh", "reboot", "factory_reset", "config_push", "firmware"}
    if action not in valid_actions:
        raise ValueError(f"Invalid bulk action: {action}")

    from app.celery_app import enqueue_celery_task
    from app.tasks.tr069 import execute_bulk_action

    task = enqueue_celery_task(
        execute_bulk_action,
        args=[device_ids, action, params or {}],
        correlation_id=f"tr069_bulk:{action}:{len(device_ids)}",
        source="web_network_tr069",
    )
    logger.info(
        "tr069_bulk_action_queued",
        extra={
            "event": "tr069_bulk_action",
            "action": action,
            "device_count": len(device_ids),
            "task_id": str(task.id),
            "correlation_id": f"tr069_bulk:{action}:{len(device_ids)}",
        },
    )
    return str(task.id)


def create_nat_port_forward_job(
    db: Session,
    *,
    tr069_device_id: str,
    external_port: int,
    internal_ip: str,
    internal_port: int,
    protocol: str,
    description: str | None = None,
) -> Tr069Job:
    """Create a NAT port forwarding rule on a TR-069 device.

    Args:
        db: Database session
        tr069_device_id: TR-069 CPE device ID
        external_port: External/WAN port number
        internal_ip: Internal/LAN IP address to forward to
        internal_port: Internal/LAN port number
        protocol: Protocol (TCP, UDP, or TCP/UDP)
        description: Optional description for the rule

    Returns:
        Created job
    """
    device = db.get(Tr069CpeDevice, coerce_uuid(tr069_device_id))
    if not device:
        raise ValueError("TR-069 device not found")

    # Validate inputs
    if not (1 <= external_port <= 65535):
        raise ValueError("External port must be between 1 and 65535")
    if not (1 <= internal_port <= 65535):
        raise ValueError("Internal port must be between 1 and 65535")

    protocol_upper = protocol.upper()
    if protocol_upper not in {"TCP", "UDP", "TCP/UDP"}:
        raise ValueError("Protocol must be TCP, UDP, or TCP/UDP")

    # Validate IP format (basic check)
    ip_parts = internal_ip.split(".")
    if len(ip_parts) != 4:
        raise ValueError("Invalid internal IP address format")
    for part in ip_parts:
        try:
            num = int(part)
            if not (0 <= num <= 255):
                raise ValueError("Invalid internal IP address format")
        except ValueError:
            raise ValueError("Invalid internal IP address format")

    # Build port mapping parameters using TR-181 paths
    # GenieACS uses addObject to create new instances
    rule_description = description or f"Port {external_port} to {internal_ip}:{internal_port}"

    # First, we need to add an object, then set its values
    # This is a two-step process in TR-069, but GenieACS can handle it with
    # setParameterValues on a new index if we use the right approach
    # For simplicity, we'll use a provision script approach or direct SPV

    # TR-181 PortMapping structure
    # Device.NAT.PortMapping.{i}.Enable
    # Device.NAT.PortMapping.{i}.Protocol
    # Device.NAT.PortMapping.{i}.ExternalPort
    # Device.NAT.PortMapping.{i}.InternalClient
    # Device.NAT.PortMapping.{i}.InternalPort
    # Device.NAT.PortMapping.{i}.Description

    # Use setParameterValues with a new instance indicator
    # GenieACS handles addObject internally when setting on non-existent instance
    parameter_values: list[list[object]] = [
        ["Device.NAT.PortMapping.*.Enable", True, "xsd:boolean"],
        ["Device.NAT.PortMapping.*.Protocol", protocol_upper, "xsd:string"],
        ["Device.NAT.PortMapping.*.ExternalPort", external_port, "xsd:unsignedInt"],
        ["Device.NAT.PortMapping.*.InternalClient", internal_ip, "xsd:string"],
        ["Device.NAT.PortMapping.*.InternalPort", internal_port, "xsd:unsignedInt"],
        ["Device.NAT.PortMapping.*.Description", rule_description, "xsd:string"],
    ]

    payload = Tr069JobCreate(
        device_id=coerce_uuid(tr069_device_id),
        name=f"NAT Forward: {external_port} → {internal_ip}:{internal_port}",
        command="setParameterValues",
        payload={
            "parameterValues": parameter_values,
        },
    )
    job = tr069_service.jobs.create(db=db, payload=payload)

    # Execute immediately
    return tr069_service.jobs.execute(db=db, job_id=str(job.id))
