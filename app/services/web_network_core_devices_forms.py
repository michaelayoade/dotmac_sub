"""Form, validation, probe, and detail helpers for core-network devices."""

from __future__ import annotations

import ipaddress
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

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
from app.models.catalog import NasConfigBackup, NasDevice
from app.services import network as network_service

logger = logging.getLogger(__name__)


def _format_uptime_short(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 0:
        return None
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


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


def _descendant_ids(db: Session, root_device_id: UUID) -> set[UUID]:
    """Return all descendant ids for a device."""
    descendants: set[UUID] = set()
    frontier: list[UUID] = [root_device_id]
    while frontier:
        parent_ids = frontier
        frontier = []
        children = list(
            db.scalars(
                select(NetworkDevice.id)
                .where(NetworkDevice.parent_device_id.in_(parent_ids))
                .where(NetworkDevice.is_active.is_(True))
            ).all()
        )
        for child_id in children:
            if child_id in descendants:
                continue
            descendants.add(child_id)
            frontier.append(child_id)
    return descendants


def parent_devices_for_forms(
    db: Session,
    *,
    current_device_id: UUID | None = None,
    pop_site_id: UUID | None = None,
) -> list[NetworkDevice]:
    """Return potential parent devices for create/edit forms."""
    stmt = (
        select(NetworkDevice)
        .where(NetworkDevice.is_active.is_(True))
        .order_by(NetworkDevice.name)
    )
    if pop_site_id:
        stmt = stmt.where(NetworkDevice.pop_site_id == pop_site_id)
    if current_device_id:
        excluded_ids = _descendant_ids(db, current_device_id) | {current_device_id}
        stmt = stmt.where(NetworkDevice.id.notin_(excluded_ids))
    return list(db.scalars(stmt).all())


def get_device(db: Session, device_id: str) -> NetworkDevice | None:
    """Return core device by id."""
    return db.scalars(select(NetworkDevice).where(NetworkDevice.id == device_id)).first()


def build_form_context(
    *,
    device: NetworkDevice | SimpleNamespace | None,
    pop_sites: list[PopSite],
    parent_devices: list[NetworkDevice],
    selected_pop_site_id: str | None,
    current_device_id: str | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared template context for core-device form."""
    context: dict[str, object] = {
        "device": device,
        "pop_sites": pop_sites,
        "parent_devices": parent_devices,
        "selected_pop_site_id": selected_pop_site_id,
        "current_device_id": current_device_id,
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
        "parent_device_id": (_form_str(form, "parent_device_id") or None),
        "ping_enabled": _form_str(form, "ping_enabled") == "true",
        "snmp_enabled": _form_str(form, "snmp_enabled") == "true",
        "send_notifications": _form_str(form, "send_notifications") == "true",
        "notification_delay_value": _form_str(form, "notification_delay_minutes"),
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
    parent_device_id = values.get("parent_device_id")
    ping_enabled = bool(values.get("ping_enabled"))
    snmp_enabled = bool(values.get("snmp_enabled"))
    send_notifications = bool(values.get("send_notifications"))
    notification_delay_value = str(values.get("notification_delay_value") or "")
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

    notification_delay_minutes: int
    if notification_delay_value:
        try:
            notification_delay_minutes = int(notification_delay_value)
        except ValueError:
            return None, "Notification delay must be a valid number of minutes"
    else:
        notification_delay_minutes = 0
    if notification_delay_minutes < 0:
        return None, "Notification delay must be zero or greater"
    if notification_delay_minutes > 10080:
        return None, "Notification delay cannot exceed 10080 minutes (7 days)"
    if not send_notifications and notification_delay_minutes:
        notification_delay_minutes = 0

    if pop_site_id:
        pop_site = db.scalars(select(PopSite).where(PopSite.id == pop_site_id)).first()
        if not pop_site:
            return None, "Selected POP site was not found"

    parent_uuid: UUID | None = None
    if parent_device_id:
        try:
            parent_uuid = UUID(str(parent_device_id))
        except ValueError:
            return None, "Invalid parent device selected"
        parent_device = db.get(NetworkDevice, parent_uuid)
        if not parent_device:
            return None, "Selected parent device was not found"
        if current_device and parent_uuid == current_device.id:
            return None, "A device cannot be its own parent"
        if current_device:
            ancestor = parent_device
            visited: set[UUID] = set()
            while ancestor and ancestor.id not in visited:
                visited.add(ancestor.id)
                if ancestor.id == current_device.id:
                    return None, "Invalid parent selection: would create a hierarchy cycle"
                ancestor = ancestor.parent_device

    if hostname:
        hostname_stmt = select(NetworkDevice).where(NetworkDevice.hostname == hostname)
        if current_device:
            if db.scalars(hostname_stmt.where(NetworkDevice.id != current_device.id)).first():
                return None, "Hostname already exists"
        elif db.scalars(hostname_stmt).first():
            return None, "Hostname already exists"

    if mgmt_ip:
        mgmt_ip_stmt = select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        if current_device:
            if db.scalars(mgmt_ip_stmt.where(NetworkDevice.id != current_device.id)).first():
                return None, "Management IP already exists"
        elif db.scalars(mgmt_ip_stmt).first():
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
            "notification_delay_minutes": notification_delay_minutes,
            "host": host,
            "parent_device_id": parent_uuid,
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
        parent_device_id=values.get("parent_device_id"),
        vendor=values.get("vendor"),
        model=values.get("model"),
        serial_number=values.get("serial_number"),
        device_type=values.get("device_type"),
        ping_enabled=bool(values.get("ping_enabled")),
        snmp_enabled=bool(values.get("snmp_enabled")),
        send_notifications=bool(values.get("send_notifications")),
        notification_delay_minutes=values.get("notification_delay_minutes")
        or values.get("notification_delay_value")
        or 0,
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
        parent_device_id=values.get("parent_device_id"),
        vendor=values.get("vendor"),
        model=values.get("model"),
        serial_number=values.get("serial_number"),
        device_type=values.get("device_type"),
        ping_enabled=bool(values.get("ping_enabled")),
        snmp_enabled=bool(values.get("snmp_enabled")),
        send_notifications=bool(values.get("send_notifications")),
        notification_delay_minutes=cast(int, values.get("notification_delay_minutes") or 0),
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
    device.parent_device_id = cast(UUID | None, values.get("parent_device_id"))
    device.vendor = cast(str | None, values.get("vendor"))
    device.model = cast(str | None, values.get("model"))
    device.serial_number = cast(str | None, values.get("serial_number"))
    device.device_type = cast(DeviceType | None, values.get("device_type"))
    device.ping_enabled = bool(values.get("ping_enabled"))
    device.snmp_enabled = bool(values.get("snmp_enabled"))
    device.send_notifications = bool(values.get("send_notifications"))
    device.notification_delay_minutes = cast(int, values.get("notification_delay_minutes") or 0)
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
    db: Session,
    role: str | None,
    status: str | None,
    pop_site_id: str | None = None,
    search: str | None = None,
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

    pop_site_filter: str | None = None
    if pop_site_id and pop_site_id != "all":
        try:
            pop_site_uuid = UUID(pop_site_id)
            stmt = stmt.where(NetworkDevice.pop_site_id == pop_site_uuid)
            pop_site_filter = pop_site_id
        except ValueError:
            pop_site_filter = None

    search_filter = (search or "").strip()
    if search_filter:
        pattern = f"%{search_filter}%"
        stmt = stmt.where(
            NetworkDevice.name.ilike(pattern)
            | NetworkDevice.hostname.ilike(pattern)
            | NetworkDevice.mgmt_ip.ilike(pattern)
            | NetworkDevice.vendor.ilike(pattern)
            | NetworkDevice.model.ilike(pattern)
        )

    devices = db.scalars(stmt.order_by(NetworkDevice.name).limit(200)).all()
    device_ids = [device.id for device in devices]
    pop_sites = pop_sites_for_forms(db)
    child_impacts: dict[str, dict[str, int | bool]] = {}
    parent_ids = [device.id for device in devices]
    if parent_ids:
        children = list(
            db.scalars(
                select(NetworkDevice)
                .where(NetworkDevice.parent_device_id.in_(parent_ids))
                .where(NetworkDevice.is_active.is_(True))
            ).all()
        )
        for child in children:
            if not child.parent_device_id:
                continue
            key = str(child.parent_device_id)
            bucket = child_impacts.setdefault(
                key, {"total": 0, "offline": 0, "degraded": 0, "impacted": False}
            )
            bucket["total"] = int(bucket["total"]) + 1
            if child.status == DeviceStatus.offline:
                bucket["offline"] = int(bucket["offline"]) + 1
            if child.status == DeviceStatus.degraded:
                bucket["degraded"] = int(bucket["degraded"]) + 1
            bucket["impacted"] = bool(
                int(bucket["offline"]) > 0 or int(bucket["degraded"]) > 0
            )

    uptime_map: dict[str, str | None] = {}
    ping_history_map: dict[str, list[dict[str, object]]] = {}
    backup_map: dict[str, dict[str, object | None]] = {}
    if device_ids:
        latest_uptime_subq = (
            select(
                DeviceMetric.device_id,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .where(DeviceMetric.device_id.in_(device_ids))
            .where(DeviceMetric.metric_type == MetricType.uptime)
            .group_by(DeviceMetric.device_id)
            .subquery()
        )
        latest_uptime_metrics = db.scalars(
            select(DeviceMetric)
            .join(
                latest_uptime_subq,
                and_(
                    DeviceMetric.device_id == latest_uptime_subq.c.device_id,
                    DeviceMetric.recorded_at == latest_uptime_subq.c.latest,
                ),
            )
            .where(DeviceMetric.metric_type == MetricType.uptime)
        ).all()
        for metric in latest_uptime_metrics:
            uptime_map[str(metric.device_id)] = _format_uptime_short(metric.value)

        recent_ping_metrics = db.scalars(
            select(DeviceMetric)
            .where(DeviceMetric.device_id.in_(device_ids))
            .where(DeviceMetric.metric_type == MetricType.custom)
            .where(DeviceMetric.unit.in_(["ping_ms", "ping_timeout"]))
            .order_by(DeviceMetric.recorded_at.desc())
            .limit(2000)
        ).all()
        for metric in recent_ping_metrics:
            key = str(metric.device_id)
            bucket = ping_history_map.setdefault(key, [])
            if len(bucket) >= 5:
                continue
            ok = metric.unit == "ping_ms" and metric.value >= 0
            bucket.append(
                {
                    "ok": ok,
                    "label": f"{metric.value}ms" if ok else "Timeout",
                    "recorded_at": metric.recorded_at,
                }
            )

        nas_devices = db.scalars(
            select(NasDevice).where(NasDevice.network_device_id.in_(device_ids))
        ).all()
        nas_by_network_id = {str(n.network_device_id): n for n in nas_devices if n.network_device_id}
        nas_ids = [n.id for n in nas_devices]
        latest_backup_by_nas_id: dict[UUID, NasConfigBackup] = {}
        if nas_ids:
            latest_backup_subq = (
                select(
                    NasConfigBackup.nas_device_id,
                    func.max(NasConfigBackup.created_at).label("latest"),
                )
                .where(NasConfigBackup.nas_device_id.in_(nas_ids))
                .group_by(NasConfigBackup.nas_device_id)
                .subquery()
            )
            latest_backups = db.scalars(
                select(NasConfigBackup)
                .join(
                    latest_backup_subq,
                    and_(
                        NasConfigBackup.nas_device_id == latest_backup_subq.c.nas_device_id,
                        NasConfigBackup.created_at == latest_backup_subq.c.latest,
                    ),
                )
            ).all()
            latest_backup_by_nas_id = {backup.nas_device_id: backup for backup in latest_backups}

        for device in devices:
            device_key = str(device.id)
            nas_device = nas_by_network_id.get(device_key)
            latest_backup = latest_backup_by_nas_id.get(nas_device.id) if nas_device else None
            status = "none"
            if latest_backup:
                notes = (latest_backup.notes or "").lower()
                status = "failed" if ("fail" in notes or "error" in notes) else "success"
            elif nas_device and nas_device.backup_enabled:
                status = "stale"
            backup_map[device_key] = {
                "status": status,
                "last_backup_at": latest_backup.created_at if latest_backup else None,
            }
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
        "pop_sites": pop_sites,
        "pop_site_filter": pop_site_filter or "all",
        "search_filter": search_filter,
        "child_impacts": child_impacts,
        "uptime_map": uptime_map,
        "ping_history_map": ping_history_map,
        "backup_map": backup_map,
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
    child_devices = list(
        db.scalars(
            select(NetworkDevice)
            .where(NetworkDevice.parent_device_id == device.id)
            .order_by(NetworkDevice.name)
        ).all()
    )

    lineage: list[NetworkDevice] = []
    ancestor = device.parent_device
    visited_ids: set[UUID] = set()
    while ancestor and ancestor.id not in visited_ids:
        lineage.append(ancestor)
        visited_ids.add(ancestor.id)
        ancestor = ancestor.parent_device
    lineage.reverse()
    child_status_summary = {
        "total": len(child_devices),
        "offline": sum(1 for c in child_devices if c.status == DeviceStatus.offline),
        "degraded": sum(1 for c in child_devices if c.status == DeviceStatus.degraded),
    }
    child_status_summary["impacted"] = (
        child_status_summary["offline"] + child_status_summary["degraded"]
    ) > 0
    descendants_by_parent: dict[UUID, list[NetworkDevice]] = {}
    descendants_frontier = [device.id]
    descendants_visited: set[UUID] = set()
    while descendants_frontier:
        children = list(
            db.scalars(
                select(NetworkDevice)
                .where(NetworkDevice.parent_device_id.in_(descendants_frontier))
                .where(NetworkDevice.is_active.is_(True))
                .order_by(NetworkDevice.name)
            ).all()
        )
        descendants_frontier = []
        for child in children:
            if child.id in descendants_visited:
                continue
            descendants_visited.add(child.id)
            if child.parent_device_id:
                descendants_by_parent.setdefault(child.parent_device_id, []).append(child)
            descendants_frontier.append(child.id)

    def _tree(parent_id: UUID, depth: int = 0) -> list[dict[str, object]]:
        nodes: list[dict[str, object]] = []
        for child in descendants_by_parent.get(parent_id, []):
            nodes.append(
                {
                    "device": child,
                    "depth": depth,
                    "children": _tree(child.id, depth + 1),
                }
            )
        return nodes

    descendant_tree = _tree(device.id)

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

    ping_history_metrics = db.scalars(
        select(DeviceMetric)
        .where(DeviceMetric.device_id == device.id)
        .where(DeviceMetric.metric_type == MetricType.custom)
        .where(DeviceMetric.unit.in_(["ping_ms", "ping_timeout"]))
        .order_by(DeviceMetric.recorded_at.desc())
        .limit(10)
    ).all()
    ping_history = [
        {
            "ok": metric.unit == "ping_ms" and metric.value >= 0,
            "label": f"{metric.value}ms" if (metric.unit == "ping_ms" and metric.value >= 0) else "Timeout",
            "recorded_at": metric.recorded_at,
        }
        for metric in ping_history_metrics
    ]

    return {
        "device": device,
        "interfaces": interfaces,
        "selected_interface": selected_interface,
        "child_devices": child_devices,
        "child_status_summary": child_status_summary,
        "descendant_tree": descendant_tree,
        "device_lineage": lineage,
        "alerts": alerts,
        "device_health": {
            "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
            "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
            "uptime": format_duration(uptime_metric.value if uptime_metric else None),
            "rx": format_bps(rx_metric.value) if rx_metric else "--",
            "tx": format_bps(tx_metric.value) if tx_metric else "--",
            "last_seen": device.last_ping_at or device.last_snmp_at,
        },
        "ping_history": ping_history,
    }


def resolve_device_redirect(db: Session, device_id: str) -> str | None:
    """Find a device across various tables and return its detail URL."""
    device = db.scalars(select(NetworkDevice).where(NetworkDevice.id == device_id)).first()
    if device:
        return f"/admin/network/core-devices/{device_id}"

    try:
        olt = network_service.olt_devices.get(db=db, device_id=device_id)
        if olt:
            return f"/admin/network/olts/{device_id}"
    except HTTPException:
        pass

    try:
        ont = network_service.ont_units.get(db=db, unit_id=device_id)
        if ont:
            return f"/admin/network/onts/{device_id}"
    except HTTPException:
        pass

    try:
        cpe = network_service.cpe_devices.get(db=db, device_id=device_id)
        if cpe:
            return f"/admin/network/cpes/{device_id}"
    except HTTPException:
        pass

    return None
