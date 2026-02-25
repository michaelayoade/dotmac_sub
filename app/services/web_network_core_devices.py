"""Service helpers for admin core-network device web routes."""

from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx
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

_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")

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
        pop_site = db.scalars(select(PopSite).where(PopSite.id == pop_site_id)).first()
        if not pop_site:
            return None, "Selected POP site was not found"

    if hostname:
        hostname_stmt = select(NetworkDevice).where(NetworkDevice.hostname == hostname)
        if current_device:
            if db.scalars(
                hostname_stmt.where(NetworkDevice.id != current_device.id)
            ).first():
                return None, "Hostname already exists"
        else:
            if db.scalars(hostname_stmt).first():
                return None, "Hostname already exists"

    if mgmt_ip:
        mgmt_ip_stmt = select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        if current_device:
            if db.scalars(
                mgmt_ip_stmt.where(NetworkDevice.id != current_device.id)
            ).first():
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
                snapshot=snapshot_for_form(
                    values, device_id=str(device.id), status=device.status
                ),
            )
        device.last_ping_at = datetime.now(UTC)
        device.last_ping_ok = True

    if values.get("snmp_enabled"):
        snmp_error = run_snmp_probe(values)
        if snmp_error:
            return CoreDeviceSubmitResult(
                None,
                error=snmp_error,
                snapshot=snapshot_for_form(
                    values, device_id=str(device.id), status=device.status
                ),
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
            snapshot=snapshot_for_form(
                values, device_id=str(device.id), status=device.status
            ),
        )
    return CoreDeviceSubmitResult(device=device)


def list_page_data(
    db: Session, role: str | None, status: str | None
) -> dict[str, object]:
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
            select(DeviceInterface).where(
                DeviceInterface.id == interface_id,
                DeviceInterface.device_id == device.id,
            )
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
        interface_metrics_by_type = {
            metric.metric_type: metric for metric in interface_metrics
        }
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
        devices.append(
            {
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
            }
        )

    onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    for ont in onts:
        devices.append(
            {
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
            }
        )

    cpes = network_service.cpe_devices.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    for cpe in cpes:
        devices.append(
            {
                "id": str(cpe.id),
                "name": getattr(cpe, "name", None)
                or getattr(cpe, "serial_number", str(cpe.id)[:8]),
                "type": "cpe",
                "serial_number": getattr(cpe, "serial_number", None),
                "ip_address": getattr(cpe, "ip_address", None),
                "vendor": getattr(cpe, "vendor", None),
                "model": getattr(cpe, "model", None),
                "status": "online",
                "last_seen": getattr(cpe, "last_seen", None),
                "subscriber": None,
            }
        )

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
        devices = [
            d for d in devices if (d.get("status") or "").lower() == status_filter
        ]

    vendor_filter = (vendor or "").strip().lower()
    if vendor_filter:
        devices = [
            d for d in devices if (d.get("vendor") or "").lower() == vendor_filter
        ]

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


def _get_olt_health(olt_name: str) -> dict[str, Any]:
    """Fetch latest OLT health metrics from VictoriaMetrics.

    Returns a dict with cpu, temperature, memory, uptime values
    and formatted display strings. Gracefully returns empty on error.
    """
    result: dict[str, Any] = {
        "has_data": False,
        "cpu_percent": None,
        "temperature_c": None,
        "memory_percent": None,
        "uptime_seconds": None,
        "cpu_display": None,
        "temperature_display": None,
        "memory_display": None,
        "uptime_display": None,
        "temperature_status": "normal",
    }

    metrics = {
        "cpu_percent": f'olt_cpu_percent{{olt_name="{olt_name}"}}',
        "temperature_c": f'olt_temperature_celsius{{olt_name="{olt_name}"}}',
        "memory_percent": f'olt_memory_percent{{olt_name="{olt_name}"}}',
        "uptime_seconds": f'olt_uptime_seconds{{olt_name="{olt_name}"}}',
    }

    for key, query in metrics.items():
        try:
            resp = httpx.get(
                f"{_VM_URL}/api/v1/query",
                params={"query": query},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if (
                isinstance(data, dict)
                and data.get("status") == "success"
            ):
                results = data.get("data", {}).get("result", [])
                if results and results[0].get("value"):
                    val = float(results[0]["value"][1])
                    result[key] = val
                    result["has_data"] = True
        except Exception:
            continue

    # Build display strings
    if result["cpu_percent"] is not None:
        result["cpu_display"] = f"{result['cpu_percent']:.0f}%"

    if result["temperature_c"] is not None:
        temp = result["temperature_c"]
        result["temperature_display"] = f"{temp:.0f}\u00b0C"
        if temp > 65:
            result["temperature_status"] = "critical"
        elif temp > 50:
            result["temperature_status"] = "warning"

    if result["memory_percent"] is not None:
        result["memory_display"] = f"{result['memory_percent']:.0f}%"

    if result["uptime_seconds"] is not None:
        secs = int(result["uptime_seconds"])
        days = secs // 86400
        hours = (secs % 86400) // 3600
        minutes = (secs % 3600) // 60
        if days > 0:
            result["uptime_display"] = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            result["uptime_display"] = f"{hours}h {minutes}m"
        else:
            result["uptime_display"] = f"{minutes}m"

    return result


def olt_detail_page_data(db: Session, olt_id: str) -> dict[str, object] | None:
    """Return OLT detail payload with PON ports, ONT assignments, and signal data."""
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

    # Gather ONT assignments and build per-port stats
    from app.services.network.olt_polling import classify_signal, get_signal_thresholds

    warn, crit = get_signal_thresholds(db)
    ont_assignments = []
    port_stats: dict[str, dict[str, int]] = {}

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
        active_assignments = [a for a in port_assignments if a.active]
        ont_assignments.extend(active_assignments)

        # Per-port ONU summary
        p_online = 0
        p_offline = 0
        p_low_signal = 0
        for a in active_assignments:
            ont = a.ont_unit
            if not ont:
                continue
            status_val = getattr(ont, "online_status", None)
            s = status_val.value if status_val else "unknown"
            if s == "online":
                p_online += 1
            elif s == "offline":
                p_offline += 1
            quality = classify_signal(
                getattr(ont, "olt_rx_signal_dbm", None),
                warn_threshold=warn,
                crit_threshold=crit,
            )
            if quality in ("warning", "critical"):
                p_low_signal += 1
        port_stats[str(port.id)] = {
            "total": len(active_assignments),
            "online": p_online,
            "offline": p_offline,
            "low_signal": p_low_signal,
        }

    # Build signal data for each ONT assignment
    signal_data: dict[str, dict[str, object]] = {}
    total_online = 0
    total_offline = 0
    total_low_signal = 0
    for a in ont_assignments:
        ont = a.ont_unit
        if not ont:
            continue
        olt_rx = getattr(ont, "olt_rx_signal_dbm", None)
        onu_rx = getattr(ont, "onu_rx_signal_dbm", None)
        quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
        status_val = getattr(ont, "online_status", None)
        s = status_val.value if status_val else "unknown"
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        signal_data[str(ont.id)] = {
            "olt_rx_dbm": olt_rx,
            "onu_rx_dbm": onu_rx,
            "quality": quality,
            "quality_class": SIGNAL_QUALITY_CLASSES.get(
                quality, SIGNAL_QUALITY_CLASSES["unknown"]
            ),
            "status": s,
            "status_class": ONLINE_STATUS_CLASSES.get(
                s, ONLINE_STATUS_CLASSES["unknown"]
            ),
            "reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
            if reason_val
            else "",
            "distance_meters": getattr(ont, "distance_meters", None),
            "signal_updated_at": getattr(ont, "signal_updated_at", None),
        }
        if s == "online":
            total_online += 1
        elif s == "offline":
            total_offline += 1
        if quality in ("warning", "critical"):
            total_low_signal += 1

    # Load shelf/card/port hierarchy via ORM relationships
    from app.models.network import OltShelf

    shelves = list(
        db.scalars(
            select(OltShelf)
            .where(OltShelf.olt_id == olt.id)
            .order_by(OltShelf.shelf_number)
        ).all()
    )

    ont_summary = {
        "total": len(ont_assignments),
        "online": total_online,
        "offline": total_offline,
        "low_signal": total_low_signal,
    }

    # Fetch OLT hardware health from VictoriaMetrics
    olt_health = _get_olt_health(olt.name)

    # Fetch recent config backups
    from app.models.network import OltConfigBackup

    config_backups = (
        db.query(OltConfigBackup)
        .filter(OltConfigBackup.olt_device_id == olt.id)
        .order_by(OltConfigBackup.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "olt": olt,
        "pon_ports": pon_ports,
        "ont_assignments": ont_assignments,
        "signal_data": signal_data,
        "port_stats": port_stats,
        "ont_summary": ont_summary,
        "shelves": shelves,
        "warn_threshold": warn,
        "crit_threshold": crit,
        "olt_health": olt_health,
        "config_backups": config_backups,
    }


def _classify_ont_signal(ont: object, warn: float, crit: float) -> str:
    """Classify ONT signal quality for template display."""
    from app.services.network.olt_polling import classify_signal

    dbm = getattr(ont, "olt_rx_signal_dbm", None)
    return classify_signal(dbm, warn_threshold=warn, crit_threshold=crit)


SIGNAL_QUALITY_CLASSES: dict[str, str] = {
    "good": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "warning": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "critical": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

ONLINE_STATUS_CLASSES: dict[str, str] = {
    "online": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "offline": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

OFFLINE_REASON_DISPLAY: dict[str, str] = {
    "power_fail": "Power Fail",
    "los": "Loss of Signal",
    "dying_gasp": "Dying Gasp",
    "unknown": "Unknown",
}


def onts_list_page_data(
    db: Session,
    *,
    status: str | None = None,
    olt_id: str | None = None,
    zone_id: str | None = None,
    online_status: str | None = None,
    signal_quality: str | None = None,
    search: str | None = None,
    vendor: str | None = None,
    order_by: str = "serial_number",
    order_dir: str = "asc",
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return ONT/CPE list payload with advanced filtering and signal classification."""
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.olt_polling import get_signal_thresholds

    # Determine is_active from status filter
    status_filter = (status or "all").strip().lower()
    is_active: bool | None = None
    if status_filter == "active":
        is_active = True
    elif status_filter == "inactive":
        is_active = False

    # Calculate pagination offset
    offset = (max(page, 1) - 1) * per_page

    # Use advanced query with all filters
    onts: Sequence[OntUnit]
    onts, total_filtered = network_service.ont_units.list_advanced(
        db,
        olt_id=olt_id,
        zone_id=zone_id,
        signal_quality=signal_quality,
        online_status=online_status,
        vendor=vendor,
        search=search,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=per_page,
        offset=offset,
    )

    # Signal threshold classification for displayed ONTs
    warn, crit = get_signal_thresholds(db)
    signal_data: dict[str, dict[str, str]] = {}
    for ont in onts:
        quality = _classify_ont_signal(ont, warn, crit)
        ont_status_enum = getattr(ont, "online_status", None)
        status_val = ont_status_enum.value if ont_status_enum else "unknown"
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        signal_data[str(ont.id)] = {
            "quality": quality,
            "quality_class": SIGNAL_QUALITY_CLASSES.get(
                quality, SIGNAL_QUALITY_CLASSES["unknown"]
            ),
            "status_class": ONLINE_STATUS_CLASSES.get(
                status_val, ONLINE_STATUS_CLASSES["unknown"]
            ),
            "status_display": status_val.replace("_", " ").title()
            if status_val
            else "Unknown",
            "reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
            if reason_val
            else "",
        }

    # Summary counts (unfiltered) for KPI cards
    all_onts_count = db.scalar(select(func.count()).select_from(OntUnit)) or 0
    from app.models.network import OnuOnlineStatus

    online_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(OntUnit.online_status == OnuOnlineStatus.online)
        )
        or 0
    )
    offline_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(OntUnit.online_status == OnuOnlineStatus.offline)
        )
        or 0
    )
    low_signal_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(OntUnit.olt_rx_signal_dbm < warn)
            .where(OntUnit.olt_rx_signal_dbm.isnot(None))
        )
        or 0
    )

    # CPEs (unchanged)
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
        "total_onts": all_onts_count,
        "total_cpes": len(cpes),
        "total": all_onts_count + len(cpes),
        "online_count": online_count,
        "offline_count": offline_count,
        "low_signal_count": low_signal_count,
    }

    # OLT list for filter dropdown
    olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()
    )

    # Zone list for filter dropdown
    from app.models.network import NetworkZone

    zones = list(
        db.scalars(
            select(NetworkZone)
            .where(NetworkZone.is_active.is_(True))
            .order_by(NetworkZone.name)
        ).all()
    )

    # Build active assignment lookup for OLT/PON display
    from app.models.network import OntAssignment, PonPort

    ont_ids = [ont.id for ont in onts]
    assignment_info: dict[str, dict[str, str]] = {}
    if ont_ids:
        assign_rows = db.execute(
            select(
                OntAssignment.ont_unit_id,
                OLTDevice.name.label("olt_name"),
                OLTDevice.id.label("olt_id"),
                PonPort.name.label("pon_port_name"),
            )
            .join(PonPort, PonPort.id == OntAssignment.pon_port_id)
            .join(OLTDevice, OLTDevice.id == PonPort.olt_id)
            .where(OntAssignment.active.is_(True))
            .where(OntAssignment.ont_unit_id.in_(ont_ids))
        ).all()
        for row in assign_rows:
            assignment_info[str(row.ont_unit_id)] = {
                "olt_name": row.olt_name,
                "olt_id": str(row.olt_id),
                "pon_port_name": row.pon_port_name,
            }

    # Pagination metadata
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)

    # Distinct vendors for filter dropdown
    vendor_rows = db.scalars(
        select(OntUnit.vendor)
        .where(OntUnit.vendor.isnot(None))
        .where(OntUnit.vendor != "")
        .distinct()
        .order_by(OntUnit.vendor)
    ).all()

    return {
        "onts": onts,
        "cpes": cpes,
        "stats": stats,
        "status_filter": status_filter,
        "signal_data": signal_data,
        "assignment_info": assignment_info,
        "olts": olts,
        "vendors": list(vendor_rows),
        # Active filters for template state
        "zones": zones,
        # Active filters for template state
        "filters": {
            "olt_id": olt_id or "",
            "zone_id": zone_id or "",
            "online_status": online_status or "",
            "signal_quality": signal_quality or "",
            "search": search or "",
            "vendor": vendor or "",
            "order_by": order_by,
            "order_dir": order_dir,
        },
        # Pagination
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total_filtered,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }


def ont_detail_page_data(db: Session, ont_id: str) -> dict[str, object] | None:
    """Return comprehensive ONT detail payload.

    Includes: device info, active assignment, OLT/PON path, subscriber,
    subscription, signal classification, and network location.
    """
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
    past_assignments = [a for a in assignments if not a.active]

    # Signal classification
    from app.services.network.olt_polling import classify_signal, get_signal_thresholds

    warn, crit = get_signal_thresholds(db)
    olt_rx = getattr(ont, "olt_rx_signal_dbm", None)
    onu_rx = getattr(ont, "onu_rx_signal_dbm", None)
    olt_quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
    onu_quality = classify_signal(onu_rx, warn_threshold=warn, crit_threshold=crit)
    ont_status = getattr(ont, "online_status", None)
    status_val = ont_status.value if ont_status else "unknown"
    reason = getattr(ont, "offline_reason", None)
    reason_val = reason.value if reason else None

    signal_info = {
        "olt_rx_dbm": olt_rx,
        "onu_rx_dbm": onu_rx,
        "olt_quality": olt_quality,
        "onu_quality": onu_quality,
        "olt_quality_class": SIGNAL_QUALITY_CLASSES.get(
            olt_quality, SIGNAL_QUALITY_CLASSES["unknown"]
        ),
        "onu_quality_class": SIGNAL_QUALITY_CLASSES.get(
            onu_quality, SIGNAL_QUALITY_CLASSES["unknown"]
        ),
        "distance_meters": getattr(ont, "distance_meters", None),
        "signal_updated_at": getattr(ont, "signal_updated_at", None),
        "online_status": status_val,
        "online_status_class": ONLINE_STATUS_CLASSES.get(
            status_val, ONLINE_STATUS_CLASSES["unknown"]
        ),
        "last_seen_at": getattr(ont, "last_seen_at", None),
        "offline_reason": reason_val,
        "offline_reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, "")
        if reason_val
        else "",
        "warn_threshold": warn,
        "crit_threshold": crit,
    }

    # Build network path info (OLT → PON Port → Splitter → ONT)
    network_path: dict[str, object] = {}
    if assignment and assignment.pon_port:
        pon_port = assignment.pon_port
        network_path["pon_port"] = pon_port.name
        if pon_port.olt:
            network_path["olt_name"] = pon_port.olt.name
            network_path["olt_id"] = str(pon_port.olt.id)
            network_path["olt_vendor"] = pon_port.olt.vendor
        # Check for splitter link
        if hasattr(pon_port, "splitter_link") and pon_port.splitter_link:
            link = pon_port.splitter_link
            if hasattr(link, "splitter_port") and link.splitter_port:
                sp = link.splitter_port
                if hasattr(sp, "splitter") and sp.splitter:
                    network_path["splitter_name"] = sp.splitter.name or str(sp.splitter.id)[:8]

    # Subscriber and subscription info
    subscriber_info: dict[str, object] = {}
    if assignment and assignment.subscriber:
        sub = assignment.subscriber
        subscriber_info["id"] = str(sub.id)
        subscriber_info["name"] = _subscriber_display_name(sub)
        subscriber_info["status"] = sub.status.value if sub.status else "unknown"
        subscriber_info["status_class"] = ONLINE_STATUS_CLASSES.get(
            "online" if subscriber_info["status"] == "active" else "offline",
            ONLINE_STATUS_CLASSES["unknown"],
        )
    if assignment and assignment.subscription:
        subscription = assignment.subscription
        subscriber_info["subscription_id"] = str(subscription.id)
        subscriber_info["plan_name"] = (
            subscription.offer.name if hasattr(subscription, "offer") and subscription.offer else None
        )
        subscriber_info["subscription_status"] = (
            subscription.status.value if subscription.status else "unknown"
        )

    return {
        "ont": ont,
        "assignment": assignment,
        "past_assignments": past_assignments,
        "signal_info": signal_info,
        "network_path": network_path,
        "subscriber_info": subscriber_info,
    }


def _subscriber_display_name(subscriber: object) -> str:
    """Build display name from subscriber person or organization."""
    person = getattr(subscriber, "person", None)
    if person:
        first = getattr(person, "first_name", "") or ""
        last = getattr(person, "last_name", "") or ""
        name = f"{first} {last}".strip()
        if name:
            return name
    org = getattr(subscriber, "organization", None)
    if org:
        org_name = getattr(org, "name", None)
        if org_name:
            return str(org_name)
    return str(getattr(subscriber, "id", ""))[:8]


def get_change_request_asset(
    db: Session, asset_type: str | None, asset_id: str | None
) -> object | None:
    """Retrieve a fiber change request asset by type and id."""
    if not asset_type or not asset_id:
        return None
    from app.services import fiber_change_requests as change_requests

    _asset_type, model = change_requests._get_model(asset_type)
    return db.get(model, asset_id)


def consolidated_page_data(
    tab: str, db: Session, search: str | None = None
) -> dict[str, object]:
    """Return consolidated network-devices page payload."""
    term = (search or "").strip().lower()

    core_devices = db.scalars(
        select(NetworkDevice).order_by(NetworkDevice.name).limit(200)
    ).all()
    core_roles = {
        "core": len([d for d in core_devices if d.role and d.role.value == "core"]),
        "distribution": len(
            [d for d in core_devices if d.role and d.role.value == "distribution"]
        ),
        "access": len([d for d in core_devices if d.role and d.role.value == "access"]),
        "aggregation": len(
            [d for d in core_devices if d.role and d.role.value == "aggregation"]
        ),
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

    if term:

        def _contains(value: object | None) -> bool:
            return term in str(value or "").lower()

        core_devices = [
            device
            for device in core_devices
            if any(
                _contains(v)
                for v in [
                    device.name,
                    device.hostname,
                    device.mgmt_ip,
                    device.vendor,
                    device.model,
                    device.serial_number,
                    device.role.value if device.role else "",
                ]
            )
        ]

        olts = [
            olt
            for olt in olts
            if any(
                _contains(v)
                for v in [
                    olt.name,
                    olt.vendor,
                    olt.model,
                    olt.mgmt_ip,
                    getattr(olt, "management_ip", None),
                    getattr(olt, "location", None),
                ]
            )
        ]

        onts = [
            ont
            for ont in onts
            if any(
                _contains(v)
                for v in [
                    getattr(ont, "serial_number", None),
                    getattr(ont, "vendor", None),
                    getattr(ont, "model", None),
                    getattr(ont, "firmware_version", None),
                    getattr(ont, "notes", None),
                ]
            )
        ]

        cpes = [
            cpe
            for cpe in cpes
            if any(
                _contains(v)
                for v in [
                    getattr(cpe, "serial_number", None),
                    getattr(cpe, "vendor", None),
                    getattr(cpe, "model", None),
                    getattr(cpe, "mac_address", None),
                    getattr(cpe, "hostname", None),
                    getattr(cpe, "management_ip", None),
                    getattr(cpe, "wan_ip", None),
                    getattr(cpe, "ssid", None),
                    getattr(cpe, "notes", None),
                ]
            )
        ]

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
        "search": search or "",
        "stats": stats,
        "core_devices": core_devices,
        "olts": olts,
        "olt_stats": olt_stats,
        "onts": onts,
        "cpes": cpes,
    }
