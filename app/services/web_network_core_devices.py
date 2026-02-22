"""Service helpers for admin core-network device web routes."""

from __future__ import annotations

import ipaddress
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import CPEDevice
from app.models.network_monitoring import (
    Alert,
    AlertStatus,
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    MetricType,
    NetworkDevice,
    PopSite,
)
from app.services import network as network_service

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models.network import Port


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


@dataclass
class CoreDeviceSubmitResult:
    """Result of create/update flow for core device forms."""

    device: NetworkDevice | None
    error: str | None = None
    snapshot: SimpleNamespace | None = None


def build_ping_command(host: str) -> list[str]:
    """Build a cross-platform ping command."""
    command = ["ping", "-c", "1", "-W", "2", host]
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 6:
            return ["ping", "-6", "-c", "1", "-W", "2", host]
    except ValueError:
        pass
    return command


def integrity_error_message(exc: Exception) -> str:
    """Map DB constraint errors to user-facing messages."""
    message = str(exc)
    if "uq_network_devices_hostname" in message:
        return "Hostname already exists"
    if "uq_network_devices_mgmt_ip" in message:
        return "Management IP already exists"
    return "Device could not be saved due to a data conflict"


def pop_sites_for_forms(db: Session) -> list[PopSite]:
    """Return active POP sites for create/edit forms."""
    return list(
        db.scalars(
            select(PopSite).where(PopSite.is_active.is_(True)).order_by(PopSite.name)
        ).all()
    )


def get_device(db: Session, device_id: str) -> NetworkDevice | None:
    """Return core device by id."""
    return db.scalars(
        select(NetworkDevice).where(NetworkDevice.id == device_id)
    ).first()


def build_form_context(
    *,
    device: NetworkDevice | SimpleNamespace | None,
    pop_sites: list[PopSite],
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared template context for core-device form."""
    context: dict[str, object] = {
        "device": device,
        "pop_sites": pop_sites,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse core-device form values into normalized strings/bools."""
    return {
        "name": _form_str(form, "name"),
        "hostname": (_form_str(form, "hostname") or None),
        "mgmt_ip": (_form_str(form, "mgmt_ip") or None),
        "role_value": _form_str(form, "role"),
        "device_type_value": _form_str(form, "device_type"),
        "pop_site_id": (_form_str(form, "pop_site_id") or None),
        "ping_enabled": _form_str(form, "ping_enabled") == "true",
        "snmp_enabled": _form_str(form, "snmp_enabled") == "true",
        "snmp_port_value": _form_str(form, "snmp_port"),
        "vendor": (_form_str(form, "vendor") or None),
        "model": (_form_str(form, "model") or None),
        "serial_number": (_form_str(form, "serial_number") or None),
        "snmp_version": (_form_str(form, "snmp_version") or None),
        "snmp_community": (_form_str(form, "snmp_community") or None),
        "snmp_username": (_form_str(form, "snmp_username") or None),
        "snmp_auth_protocol": (_form_str(form, "snmp_auth_protocol") or None),
        "snmp_auth_secret": (_form_str(form, "snmp_auth_secret") or None),
        "snmp_priv_protocol": (_form_str(form, "snmp_priv_protocol") or None),
        "snmp_priv_secret": (_form_str(form, "snmp_priv_secret") or None),
        "notes": (_form_str(form, "notes") or None),
        "is_active": _form_str(form, "is_active") == "true",
    }


def validate_values(
    db: Session,
    values: dict[str, object],
    *,
    current_device: NetworkDevice | None = None,
) -> tuple[dict[str, object] | None, str | None]:
    """Validate and normalize core-device form fields."""
    name = str(values.get("name") or "")
    hostname = values.get("hostname")
    mgmt_ip = values.get("mgmt_ip")
    role_value = str(values.get("role_value") or "")
    device_type_value = str(values.get("device_type_value") or "")
    pop_site_id = values.get("pop_site_id")
    ping_enabled = bool(values.get("ping_enabled"))
    snmp_enabled = bool(values.get("snmp_enabled"))
    snmp_port_value = str(values.get("snmp_port_value") or "")

    if not name:
        return None, "Device name is required"

    try:
        role = DeviceRole(role_value)
    except ValueError:
        return None, "Invalid device role"

    try:
        device_type = DeviceType(device_type_value) if device_type_value else None
    except ValueError:
        return None, "Invalid device type"

    snmp_port: int | None
    if snmp_port_value:
        try:
            snmp_port = int(snmp_port_value)
        except ValueError:
            return None, "SNMP port must be a valid number"
    else:
        snmp_port = 161 if snmp_enabled else None

    if pop_site_id:
        pop_site = db.scalars(
            select(PopSite).where(PopSite.id == pop_site_id)
        ).first()
        if not pop_site:
            return None, "Selected POP site was not found"

    if hostname:
        hostname_stmt = select(NetworkDevice).where(NetworkDevice.hostname == hostname)
        if current_device:
            if db.scalars(hostname_stmt.where(NetworkDevice.id != current_device.id)).first():
                return None, "Hostname already exists"
        else:
            if db.scalars(hostname_stmt).first():
                return None, "Hostname already exists"

    if mgmt_ip:
        mgmt_ip_stmt = select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        if current_device:
            if db.scalars(mgmt_ip_stmt.where(NetworkDevice.id != current_device.id)).first():
                return None, "Management IP already exists"
        else:
            if db.scalars(mgmt_ip_stmt).first():
                return None, "Management IP already exists"

    host = mgmt_ip or hostname
    if ping_enabled and not host:
        return None, "Management IP or hostname is required for ping checks."
    if snmp_enabled and not host:
        return None, "Management IP or hostname is required for SNMP checks."

    normalized = dict(values)
    normalized.update(
        {
            "role": role,
            "device_type": device_type,
            "snmp_port": snmp_port,
            "host": host,
        }
    )
    return normalized, None


def snapshot_for_form(
    values: dict[str, object],
    *,
    status: DeviceStatus | None = None,
    device_id: str | None = None,
) -> SimpleNamespace:
    """Build a template-friendly device snapshot for validation errors."""
    return SimpleNamespace(
        id=device_id,
        name=values.get("name"),
        hostname=values.get("hostname"),
        mgmt_ip=values.get("mgmt_ip"),
        role=values.get("role") or DeviceRole.edge,
        status=status or DeviceStatus.offline,
        pop_site_id=values.get("pop_site_id"),
        vendor=values.get("vendor"),
        model=values.get("model"),
        serial_number=values.get("serial_number"),
        device_type=values.get("device_type"),
        ping_enabled=bool(values.get("ping_enabled")),
        snmp_enabled=bool(values.get("snmp_enabled")),
        snmp_port=values.get("snmp_port"),
        snmp_version=values.get("snmp_version"),
        snmp_community=values.get("snmp_community"),
        snmp_username=values.get("snmp_username"),
        snmp_auth_protocol=values.get("snmp_auth_protocol"),
        snmp_auth_secret=values.get("snmp_auth_secret"),
        snmp_priv_protocol=values.get("snmp_priv_protocol"),
        snmp_priv_secret=values.get("snmp_priv_secret"),
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )


def run_ping_probe(host: str) -> bool:
    """Run ping probe for host reachability validation."""
    try:
        result = subprocess.run(
            build_ping_command(host),
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        )
        return result.returncode == 0
    except Exception:
        return False


def run_snmp_probe(values: dict[str, object]) -> str | None:
    """Run lightweight SNMP probe; return error message on failure."""
    from app.services.snmp_discovery import _run_snmpwalk

    probe = NetworkDevice(
        name="SNMP Probe",
        hostname=cast(str | None, values.get("hostname")),
        mgmt_ip=cast(str | None, values.get("mgmt_ip")),
        role=DeviceRole.edge,
        status=DeviceStatus.offline,
        ping_enabled=False,
        snmp_enabled=True,
        snmp_port=cast(int | None, values.get("snmp_port")),
        snmp_version=cast(str | None, values.get("snmp_version")),
        snmp_community=cast(str | None, values.get("snmp_community")),
        snmp_username=cast(str | None, values.get("snmp_username")),
        snmp_auth_protocol=cast(str | None, values.get("snmp_auth_protocol")),
        snmp_auth_secret=cast(str | None, values.get("snmp_auth_secret")),
        snmp_priv_protocol=cast(str | None, values.get("snmp_priv_protocol")),
        snmp_priv_secret=cast(str | None, values.get("snmp_priv_secret")),
        is_active=True,
    )
    try:
        _run_snmpwalk(probe, ".1.3.6.1.2.1.1.3.0", timeout=8)
    except Exception as exc:
        return f"SNMP check failed: {str(exc)}"
    return None


def create_device(db: Session, values: dict[str, object]) -> CoreDeviceSubmitResult:
    """Create a core device with optional ping/SNMP probes."""
    status = DeviceStatus.offline
    device = NetworkDevice(
        name=values["name"],
        hostname=values.get("hostname"),
        mgmt_ip=values.get("mgmt_ip"),
        role=values["role"],
        status=status,
        pop_site_id=values.get("pop_site_id"),
        vendor=values.get("vendor"),
        model=values.get("model"),
        serial_number=values.get("serial_number"),
        device_type=values.get("device_type"),
        ping_enabled=bool(values.get("ping_enabled")),
        snmp_enabled=bool(values.get("snmp_enabled")),
        snmp_port=values.get("snmp_port"),
        snmp_version=values.get("snmp_version"),
        snmp_community=values.get("snmp_community"),
        snmp_username=values.get("snmp_username"),
        snmp_auth_protocol=values.get("snmp_auth_protocol"),
        snmp_auth_secret=values.get("snmp_auth_secret"),
        snmp_priv_protocol=values.get("snmp_priv_protocol"),
        snmp_priv_secret=values.get("snmp_priv_secret"),
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )

    host = values.get("host")
    if values.get("ping_enabled") and host:
        if not run_ping_probe(str(host)):
            return CoreDeviceSubmitResult(
                None,
                error="Ping failed. Check the management IP/hostname.",
                snapshot=snapshot_for_form(values, status=status),
            )
        device.last_ping_at = datetime.now(UTC)
        device.last_ping_ok = True

    if values.get("snmp_enabled"):
        snmp_error = run_snmp_probe(values)
        if snmp_error:
            return CoreDeviceSubmitResult(
                None,
                error=snmp_error,
                snapshot=snapshot_for_form(values, status=status),
            )
        device.last_snmp_at = datetime.now(UTC)
        device.last_snmp_ok = True

    db.add(device)
    try:
        db.commit()
        db.refresh(device)
    except IntegrityError as exc:
        db.rollback()
        return CoreDeviceSubmitResult(
            None,
            error=integrity_error_message(exc),
            snapshot=snapshot_for_form(values, status=status),
        )
    return CoreDeviceSubmitResult(device=device)


def update_device(
    db: Session, device: NetworkDevice, values: dict[str, object]
) -> CoreDeviceSubmitResult:
    """Update a core device with optional ping/SNMP probes."""
    if values.get("ping_enabled") and values.get("host"):
        if not run_ping_probe(str(values["host"])):
            return CoreDeviceSubmitResult(
                None,
                error="Ping failed. Check the management IP/hostname.",
                snapshot=snapshot_for_form(values, device_id=str(device.id), status=device.status),
            )
        device.last_ping_at = datetime.now(UTC)
        device.last_ping_ok = True

    if values.get("snmp_enabled"):
        snmp_error = run_snmp_probe(values)
        if snmp_error:
            return CoreDeviceSubmitResult(
                None,
                error=snmp_error,
                snapshot=snapshot_for_form(values, device_id=str(device.id), status=device.status),
            )
        device.last_snmp_at = datetime.now(UTC)
        device.last_snmp_ok = True

    device.name = cast(str, values["name"])
    device.hostname = cast(str | None, values.get("hostname"))
    device.mgmt_ip = cast(str | None, values.get("mgmt_ip"))
    device.role = cast(DeviceRole, values["role"])
    device.pop_site_id = cast(UUID | None, values.get("pop_site_id"))
    device.vendor = cast(str | None, values.get("vendor"))
    device.model = cast(str | None, values.get("model"))
    device.serial_number = cast(str | None, values.get("serial_number"))
    device.device_type = cast(DeviceType | None, values.get("device_type"))
    device.ping_enabled = bool(values.get("ping_enabled"))
    device.snmp_enabled = bool(values.get("snmp_enabled"))
    device.snmp_port = cast(int | None, values.get("snmp_port"))
    device.snmp_version = cast(str | None, values.get("snmp_version"))
    device.snmp_community = cast(str | None, values.get("snmp_community"))
    device.snmp_username = cast(str | None, values.get("snmp_username"))
    device.snmp_auth_protocol = cast(str | None, values.get("snmp_auth_protocol"))
    device.snmp_auth_secret = cast(str | None, values.get("snmp_auth_secret"))
    device.snmp_priv_protocol = cast(str | None, values.get("snmp_priv_protocol"))
    device.snmp_priv_secret = cast(str | None, values.get("snmp_priv_secret"))
    device.notes = cast(str | None, values.get("notes"))
    device.is_active = bool(values.get("is_active"))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        return CoreDeviceSubmitResult(
            None,
            error=integrity_error_message(exc),
            snapshot=snapshot_for_form(values, device_id=str(device.id), status=device.status),
        )
    return CoreDeviceSubmitResult(device=device)


def list_page_data(db: Session, role: str | None, status: str | None) -> dict[str, object]:
    """Return data payload for core-device index."""
    stmt = select(NetworkDevice)
    if role:
        try:
            role_enum = DeviceRole(role)
            stmt = stmt.where(NetworkDevice.role == role_enum)
        except ValueError:
            pass

    status_filter = (status or "all").strip().lower()
    if status_filter == "active":
        stmt = stmt.where(NetworkDevice.is_active.is_(True))
    elif status_filter == "inactive":
        stmt = stmt.where(NetworkDevice.is_active.is_(False))

    devices = db.scalars(stmt.order_by(NetworkDevice.name).limit(200)).all()
    stats = {
        "total": len(devices),
        "core": sum(1 for d in devices if d.role.value == "core"),
        "distribution": sum(1 for d in devices if d.role.value == "distribution"),
        "access": sum(1 for d in devices if d.role.value == "access"),
        "aggregation": sum(1 for d in devices if d.role.value == "aggregation"),
        "edge": sum(1 for d in devices if d.role.value == "edge"),
    }
    return {
        "devices": devices,
        "stats": stats,
        "role_filter": role,
        "status_filter": status_filter,
    }


def detail_page_data(
    db: Session,
    device_id: str,
    interface_id: str | None,
    *,
    format_duration: Callable[[float | int | None], str],
    format_bps: Callable[[float | int | None], str],
) -> dict[str, object] | None:
    """Return full payload for core-device detail page."""
    device = get_device(db, device_id)
    if not device:
        return None

    interfaces = db.scalars(
        select(DeviceInterface)
        .where(DeviceInterface.device_id == device.id)
        .order_by(DeviceInterface.name)
    ).all()
    selected_interface = None
    if interface_id:
        selected_interface = db.scalars(
            select(DeviceInterface)
            .where(DeviceInterface.id == interface_id, DeviceInterface.device_id == device.id)
        ).first()

    alerts = db.scalars(
        select(Alert)
        .where(
            Alert.device_id == device.id,
            Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]),
        )
        .order_by(Alert.triggered_at.desc())
        .limit(10)
    ).all()

    metric_types = [MetricType.cpu, MetricType.memory, MetricType.uptime]
    if not selected_interface:
        metric_types.extend([MetricType.rx_bps, MetricType.tx_bps])

    latest_metrics_subq = (
        select(
            DeviceMetric.metric_type,
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .where(DeviceMetric.device_id == device.id)
        .where(DeviceMetric.metric_type.in_(metric_types))
        .group_by(DeviceMetric.metric_type)
        .subquery()
    )
    latest_metrics = db.scalars(
        select(DeviceMetric)
        .join(
            latest_metrics_subq,
            and_(
                DeviceMetric.metric_type == latest_metrics_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_metrics_subq.c.latest,
            ),
        )
        .where(DeviceMetric.device_id == device.id)
    ).all()
    metrics_by_type = {metric.metric_type: metric for metric in latest_metrics}

    cpu_metric = metrics_by_type.get(MetricType.cpu)
    mem_metric = metrics_by_type.get(MetricType.memory)
    uptime_metric = metrics_by_type.get(MetricType.uptime)
    if selected_interface:
        interface_metric_types = [MetricType.rx_bps, MetricType.tx_bps]
        interface_metrics_subq = (
            select(
                DeviceMetric.metric_type,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .where(DeviceMetric.device_id == device.id)
            .where(DeviceMetric.interface_id == selected_interface.id)
            .where(DeviceMetric.metric_type.in_(interface_metric_types))
            .group_by(DeviceMetric.metric_type)
            .subquery()
        )
        interface_metrics = db.scalars(
            select(DeviceMetric)
            .join(
                interface_metrics_subq,
                and_(
                    DeviceMetric.metric_type == interface_metrics_subq.c.metric_type,
                    DeviceMetric.recorded_at == interface_metrics_subq.c.latest,
                ),
            )
            .where(DeviceMetric.device_id == device.id)
            .where(DeviceMetric.interface_id == selected_interface.id)
        ).all()
        interface_metrics_by_type = {metric.metric_type: metric for metric in interface_metrics}
        rx_metric = interface_metrics_by_type.get(MetricType.rx_bps)
        tx_metric = interface_metrics_by_type.get(MetricType.tx_bps)
    else:
        rx_metric = metrics_by_type.get(MetricType.rx_bps)
        tx_metric = metrics_by_type.get(MetricType.tx_bps)

    return {
        "device": device,
        "interfaces": interfaces,
        "selected_interface": selected_interface,
        "alerts": alerts,
        "device_health": {
            "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
            "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
            "uptime": format_duration(uptime_metric.value if uptime_metric else None),
            "rx": format_bps(rx_metric.value) if rx_metric else "--",
            "tx": format_bps(tx_metric.value) if tx_metric else "--",
            "last_seen": device.last_ping_at or device.last_snmp_at,
        },
    }


def resolve_device_redirect(db: Session, device_id: str) -> str | None:
    """Try to find a device across various device tables and return its detail URL.

    Returns None if no device was found.
    """
    # Try core network device
    device = db.scalars(
        select(NetworkDevice).where(NetworkDevice.id == device_id)
    ).first()
    if device:
        return f"/admin/network/core-devices/{device_id}"

    # Try OLT
    try:
        olt = network_service.olt_devices.get(db=db, device_id=device_id)
        if olt:
            return f"/admin/network/olts/{device_id}"
    except HTTPException:
        pass

    # Try ONT
    try:
        ont = network_service.ont_units.get(db=db, unit_id=device_id)
        if ont:
            return f"/admin/network/onts/{device_id}"
    except HTTPException:
        pass

    # Try CPE
    try:
        cpe = network_service.cpe_devices.get(db=db, device_id=device_id)
        if cpe:
            return f"/admin/network/cpes/{device_id}"
    except HTTPException:
        pass

    return None


def get_cpe_ports(db: Session, cpe_id: object) -> list[Port]:
    """Return ports for a CPE device."""
    from app.models.network import Port

    return list(db.scalars(select(Port).where(Port.device_id == cpe_id)).all())


def collect_devices(db: Session) -> list[dict]:
    """Collect all device types into a unified list of dicts."""
    devices: list[dict] = []

    olts = network_service.olt_devices.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
    )
    for olt in olts:
        devices.append({
            "id": str(olt.id),
            "name": olt.name,
            "type": "olt",
            "serial_number": getattr(olt, "serial_number", None),
            "ip_address": getattr(olt, "mgmt_ip", None),
            "vendor": olt.vendor,
            "model": olt.model,
            "status": "online" if olt.is_active else "offline",
            "last_seen": getattr(olt, "last_seen", None),
            "subscriber": None,
        })

    onts = network_service.ont_units.list(
        db=db, is_active=True, order_by="serial_number", order_dir="asc", limit=500, offset=0
    )
    for ont in onts:
        devices.append({
            "id": str(ont.id),
            "name": getattr(ont, "name", None) or ont.serial_number,
            "type": "ont",
            "serial_number": ont.serial_number,
            "ip_address": getattr(ont, "ip_address", None),
            "vendor": ont.vendor,
            "model": ont.model,
            "status": "online" if ont.is_active else "offline",
            "last_seen": getattr(ont, "last_seen", None),
            "subscriber": None,
        })

    cpes = network_service.cpe_devices.list(
        db=db, subscriber_id=None, subscription_id=None, order_by="created_at",
        order_dir="desc", limit=500, offset=0,
    )
    for cpe in cpes:
        devices.append({
            "id": str(cpe.id),
            "name": getattr(cpe, "name", None) or getattr(cpe, "serial_number", str(cpe.id)[:8]),
            "type": "cpe",
            "serial_number": getattr(cpe, "serial_number", None),
            "ip_address": getattr(cpe, "ip_address", None),
            "vendor": getattr(cpe, "vendor", None),
            "model": getattr(cpe, "model", None),
            "status": "online",
            "last_seen": getattr(cpe, "last_seen", None),
            "subscriber": None,
        })

    return devices


def _device_matches_search(device: dict, term: str) -> bool:
    """Check if any device field matches the search term."""
    haystack = [
        device.get("name"),
        device.get("serial_number"),
        device.get("ip_address"),
        device.get("vendor"),
        device.get("model"),
        device.get("type"),
    ]
    return any((value or "").lower().find(term) != -1 for value in haystack)


def filter_devices(
    devices: list[dict],
    *,
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> list[dict]:
    """Apply optional filters to a device list."""
    if device_type and device_type != "all":
        devices = [d for d in devices if d["type"] == device_type]

    term = (search or "").strip().lower()
    if term:
        devices = [d for d in devices if _device_matches_search(d, term)]

    status_filter = (status or "").strip().lower()
    if status_filter:
        devices = [d for d in devices if (d.get("status") or "").lower() == status_filter]

    vendor_filter = (vendor or "").strip().lower()
    if vendor_filter:
        devices = [d for d in devices if (d.get("vendor") or "").lower() == vendor_filter]

    return devices


def compute_device_stats(devices: list[dict]) -> dict[str, int]:
    """Compute summary stats for a filtered device list."""
    return {
        "total": len(devices),
        "olt": sum(1 for d in devices if d["type"] == "olt"),
        "ont": sum(1 for d in devices if d["type"] == "ont"),
        "cpe": sum(1 for d in devices if d["type"] == "cpe"),
        "online": sum(1 for d in devices if d["status"] == "online"),
        "offline": sum(1 for d in devices if d["status"] == "offline"),
        "warning": 0,
        "unprovisioned": 0,
    }


def devices_list_page_data(
    db: Session,
    *,
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> dict[str, object]:
    """Return full payload for the devices index page."""
    devices = collect_devices(db)
    devices = filter_devices(
        devices, device_type=device_type, search=search, status=status, vendor=vendor
    )
    stats = compute_device_stats(devices)
    return {
        "devices": devices,
        "stats": stats,
        "device_type": device_type,
        "search": search or "",
        "status": status or "",
        "vendor": vendor or "",
    }


def devices_search_data(db: Session, search: str) -> list[dict]:
    """Return filtered devices for HTMX search partial."""
    devices = collect_devices(db)
    term = search.strip().lower()
    if term:
        devices = [d for d in devices if _device_matches_search(d, term)]
    return devices


def devices_filter_data(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> list[dict]:
    """Return filtered devices for HTMX filter partial."""
    devices = collect_devices(db)
    return filter_devices(devices, search=search, status=status, vendor=vendor)


def olts_list_page_data(db: Session) -> dict[str, object]:
    """Return OLT list payload with per-OLT stats."""
    olts = network_service.olt_devices.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    olt_stats = {}
    for olt in olts:
        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=str(olt.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        olt_stats[str(olt.id)] = {"pon_ports": len(pon_ports)}

    stats = {"total": len(olts), "active": sum(1 for o in olts if o.is_active)}

    return {"olts": olts, "olt_stats": olt_stats, "stats": stats}


def olt_detail_page_data(db: Session, olt_id: str) -> dict[str, object] | None:
    """Return OLT detail payload with PON ports and ONT assignments."""
    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except HTTPException:
        return None

    pon_ports = network_service.pon_ports.list(
        db=db,
        olt_id=olt_id,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    ont_assignments = []
    for port in pon_ports:
        port_assignments = network_service.ont_assignments.list(
            db=db,
            pon_port_id=str(port.id),
            ont_unit_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        port_assignments = [a for a in port_assignments if a.active]
        ont_assignments.extend(port_assignments)

    return {"olt": olt, "pon_ports": pon_ports, "ont_assignments": ont_assignments}


def onts_list_page_data(db: Session, status: str | None = None) -> dict[str, object]:
    """Return ONT/CPE list payload."""
    active_onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    inactive_onts = network_service.ont_units.list(
        db=db,
        is_active=False,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    all_onts = active_onts + inactive_onts

    status_filter = (status or "all").strip().lower()
    if status_filter == "active":
        onts = active_onts
    elif status_filter == "inactive":
        onts = inactive_onts
    else:
        onts = all_onts

    cpes = network_service.cpe_devices.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )

    stats = {
        "total_onts": len(all_onts),
        "active_onts": len(active_onts),
        "inactive_onts": len(inactive_onts),
        "total_cpes": len(cpes),
        "total": len(all_onts) + len(cpes),
    }

    return {
        "onts": onts,
        "cpes": cpes,
        "stats": stats,
        "status_filter": status_filter,
    }


def ont_detail_page_data(db: Session, ont_id: str) -> dict[str, object] | None:
    """Return ONT detail payload with active assignment."""
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return None

    assignments = network_service.ont_assignments.list(
        db=db,
        ont_unit_id=ont_id,
        pon_port_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assignment = next((a for a in assignments if a.active), None)

    return {"ont": ont, "assignment": assignment}


def get_change_request_asset(db: Session, asset_type: str | None, asset_id: str | None) -> object | None:
    """Retrieve a fiber change request asset by type and id."""
    if not asset_type or not asset_id:
        return None
    from app.services import fiber_change_requests as change_requests

    _asset_type, model = change_requests._get_model(asset_type)
    return db.get(model, asset_id)


def consolidated_page_data(tab: str, db: Session) -> dict[str, object]:
    """Return consolidated network-devices page payload."""
    core_devices = db.scalars(
        select(NetworkDevice).order_by(NetworkDevice.name).limit(200)
    ).all()
    core_roles = {
        "core": len([d for d in core_devices if d.role and d.role.value == "core"]),
        "distribution": len([d for d in core_devices if d.role and d.role.value == "distribution"]),
        "access": len([d for d in core_devices if d.role and d.role.value == "access"]),
        "aggregation": len([d for d in core_devices if d.role and d.role.value == "aggregation"]),
        "edge": len([d for d in core_devices if d.role and d.role.value == "edge"]),
    }

    olts = network_service.olt_devices.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    olt_stats = {}
    for olt in olts:
        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=str(olt.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        olt_stats[str(olt.id)] = {"pon_ports": len(pon_ports)}

    active_onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    inactive_onts = network_service.ont_units.list(
        db=db,
        is_active=False,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    onts = active_onts + inactive_onts
    cpes = db.scalars(
        select(CPEDevice).order_by(CPEDevice.created_at.desc()).limit(200)
    ).all()

    stats = {
        "core_total": len(core_devices),
        "core_roles": core_roles,
        "olt_total": len(olts),
        "olt_active": sum(1 for o in olts if o.is_active),
        "ont_total": len(onts),
        "ont_inactive": len(inactive_onts),
        "cpe_total": len(cpes),
    }
    return {
        "tab": tab,
        "stats": stats,
        "core_devices": core_devices,
        "olts": olts,
        "olt_stats": olt_stats,
        "onts": onts,
        "cpes": cpes,
    }
