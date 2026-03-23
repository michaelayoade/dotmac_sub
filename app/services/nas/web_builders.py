"""Web context builders for NAS admin pages."""

import json
import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    ConfigBackupMethod,
    ConnectionType,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningLogStatus,
    ProvisioningTemplate,
)
from app.models.domain_settings import SettingDomain
from app.models.network_monitoring import (
    DeviceMetric,
    DeviceStatus,
    MetricType,
    NetworkDevice,
)
from app.schemas.catalog import (
    NasDeviceCreate,
    NasDeviceUpdate,
    ProvisioningTemplateCreate,
    ProvisioningTemplateUpdate,
)
from app.services import backup_alerts as backup_alerts_service
from app.services import ping as ping_service
from app.services.audit_helpers import diff_dicts, model_to_dict
from app.services.nas._helpers import (
    RADIUS_REQUIRED_CONNECTION_TYPES,
    TEMPLATE_AUDIT_EXCLUDE_FIELDS,
    extract_enhanced_fields,
    extract_mikrotik_status,
    list_organizations,
    merge_partner_org_tags,
    merge_radius_pool_tags,
    merge_single_tag,
    pop_site_label,
    resolve_partner_org_names,
    resolve_radius_pool_names,
    validate_ipv4_address,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def build_nas_device_payload(
    db: Session,
    *,
    form: dict[str, Any],
    existing_tags: list[str] | None = None,
    for_update: bool = False,
) -> tuple[NasDeviceCreate | NasDeviceUpdate | None, list[str]]:
    """Validate web form values and build NAS create/update payload."""
    from app.services import network as network_service

    errors: list[str] = []
    supported_connection_types = form.get("supported_connection_types")
    conn_types: list[ConnectionType] | None = None
    if supported_connection_types:
        try:
            conn_types_raw = json.loads(str(supported_connection_types))
            conn_types = [ConnectionType(ct) for ct in conn_types_raw]
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"Invalid connection types: {exc}")

    ip_error = validate_ipv4_address(
        cast(str | None, form.get("ip_address")), "IP address"
    )
    if ip_error:
        errors.append(ip_error)
    nas_ip = cast(str | None, form.get("nas_ip"))
    nas_ip_error = validate_ipv4_address(nas_ip, "NAS IP")
    if nas_ip_error:
        errors.append(nas_ip_error)
    if (
        conn_types
        and any(ct in RADIUS_REQUIRED_CONNECTION_TYPES for ct in conn_types)
        and not nas_ip
    ):
        errors.append(
            "NAS IP is required when PPPoE, IPoE, or Hotspot authentication is enabled."
        )
    if (
        str(form.get("authorization_type") or "").strip().lower() == "ppp_dhcp_radius"
        and not nas_ip
    ):
        errors.append("NAS IP is required when authorization type is PPP/DHCP Radius.")

    radius_pool_ids = cast(list[str], form.get("radius_pool_ids") or [])
    if radius_pool_ids:
        valid_pool_ids = {
            str(pool.id)
            for pool in network_service.ip_pools.list(
                db=db,
                ip_version=None,
                is_active=True,
                order_by="name",
                order_dir="asc",
                limit=500,
                offset=0,
            )
        }
        for pool_id in radius_pool_ids:
            if pool_id not in valid_pool_ids:
                errors.append(f"Invalid RADIUS pool selected: {pool_id}")
                break

    partner_org_ids = cast(list[str], form.get("partner_org_ids") or [])
    if partner_org_ids:
        valid_org_ids = {str(org.id) for org in list_organizations(db, limit=5000)}
        for org_id in partner_org_ids:
            if org_id not in valid_org_ids:
                errors.append(f"Invalid organization selected: {org_id}")
                break

    latitude = cast(str | None, form.get("latitude"))
    longitude = cast(str | None, form.get("longitude"))
    if latitude:
        try:
            lat_value = float(latitude)
            if lat_value < -90 or lat_value > 90:
                errors.append("Latitude must be between -90 and 90.")
        except ValueError:
            errors.append("Latitude must be a valid number.")
    if longitude:
        try:
            lon_value = float(longitude)
            if lon_value < -180 or lon_value > 180:
                errors.append("Longitude must be between -180 and 180.")
        except ValueError:
            errors.append("Longitude must be a valid number.")

    if errors:
        return None, errors

    tags = merge_radius_pool_tags(existing_tags, radius_pool_ids)
    tags = merge_partner_org_tags(tags, partner_org_ids)
    tags = merge_single_tag(
        tags, "authorization_type:", cast(str | None, form.get("authorization_type"))
    )
    tags = merge_single_tag(
        tags, "accounting_type:", cast(str | None, form.get("accounting_type"))
    )
    tags = merge_single_tag(
        tags, "physical_address:", cast(str | None, form.get("physical_address"))
    )
    tags = merge_single_tag(tags, "latitude:", latitude)
    tags = merge_single_tag(tags, "longitude:", longitude)
    tags = merge_single_tag(
        tags,
        "mikrotik_api_enabled:",
        "true" if bool(form.get("mikrotik_api_enabled")) else "false",
    )
    tags = merge_single_tag(
        tags,
        "mikrotik_api_port:",
        str(form.get("mikrotik_api_port")) if form.get("mikrotik_api_port") else None,
    )
    tags = merge_single_tag(
        tags,
        "shaper_enabled:",
        "true" if bool(form.get("shaper_enabled")) else "false",
    )
    tags = merge_single_tag(
        tags, "shaper_target:", cast(str | None, form.get("shaper_target"))
    )
    tags = merge_single_tag(
        tags, "shaping_type:", cast(str | None, form.get("shaping_type"))
    )
    tags = merge_single_tag(
        tags,
        "wireless_access_list:",
        "true" if bool(form.get("wireless_access_list")) else "false",
    )
    tags = merge_single_tag(
        tags,
        "disabled_customers_address_list:",
        "true" if bool(form.get("disabled_customers_address_list")) else "false",
    )
    tags = merge_single_tag(
        tags,
        "blocking_rules_enabled:",
        "true" if bool(form.get("blocking_rules_enabled")) else "false",
    )

    try:
        name = cast(str | None, form.get("name"))
        code = cast(str | None, form.get("nas_identifier") or None)
        vendor = NasVendor(str(form.get("vendor")))
        model = cast(str | None, form.get("model") or None)
        ip_address = cast(str | None, form.get("ip_address"))
        management_ip = cast(str | None, form.get("ip_address"))
        management_port = cast(int | None, form.get("ssh_port"))
        description = cast(str | None, form.get("description") or None)
        pop_site_id = (
            UUID(str(form["pop_site_id"])) if form.get("pop_site_id") else None
        )
        rack_position = cast(str | None, form.get("location") or None)
        status = NasDeviceStatus(str(form.get("status")))
        supported_types = [ct.value for ct in conn_types] if conn_types else None
        default_connection_type = (
            ConnectionType(str(form["default_connection_type"]))
            if form.get("default_connection_type")
            else None
        )
        ssh_username = cast(str | None, form.get("ssh_username") or None)
        ssh_password = cast(str | None, form.get("ssh_password") or None)
        ssh_key = cast(str | None, form.get("ssh_key") or None)
        api_url = cast(str | None, form.get("api_url") or None)
        api_username = cast(str | None, form.get("api_username") or None)
        api_password = cast(str | None, form.get("api_password") or None)
        api_token = cast(str | None, form.get("api_key") or None)
        snmp_community = cast(str | None, form.get("snmp_community") or None)
        snmp_version = cast(str | None, form.get("snmp_version") or None)
        snmp_port = cast(int | None, form.get("snmp_port"))
        backup_enabled = cast(bool | None, form.get("backup_enabled"))
        backup_method = (
            ConfigBackupMethod(str(form["backup_method"]))
            if form.get("backup_method")
            else None
        )
        backup_schedule = cast(str | None, form.get("backup_schedule") or None)
        shared_secret = cast(str | None, form.get("radius_secret") or None)
        coa_port = cast(int | None, form.get("coa_port"))
        firmware_version = cast(str | None, form.get("firmware_version") or None)
        serial_number = cast(str | None, form.get("serial_number") or None)
        notes = cast(str | None, form.get("notes") or None)
        is_active = cast(bool | None, form.get("is_active"))
        if for_update:
            payload: NasDeviceCreate | NasDeviceUpdate = NasDeviceUpdate(
                name=name,
                code=code,
                vendor=vendor,
                model=model,
                ip_address=ip_address,
                management_ip=management_ip,
                management_port=management_port,
                nas_ip=nas_ip or None,
                description=description,
                pop_site_id=pop_site_id,
                rack_position=rack_position,
                status=status,
                supported_connection_types=supported_types,
                default_connection_type=default_connection_type,
                ssh_username=ssh_username,
                ssh_password=ssh_password,
                ssh_key=ssh_key,
                api_url=api_url,
                api_username=api_username,
                api_password=api_password,
                api_token=api_token,
                snmp_community=snmp_community,
                snmp_version=snmp_version,
                snmp_port=snmp_port,
                backup_enabled=backup_enabled,
                backup_method=backup_method,
                backup_schedule=backup_schedule,
                shared_secret=shared_secret,
                coa_port=coa_port,
                firmware_version=firmware_version,
                serial_number=serial_number,
                notes=notes,
                tags=tags,
                is_active=is_active,
            )
        else:
            payload = NasDeviceCreate(
                name=cast(str, name),
                code=code,
                vendor=vendor,
                model=model,
                ip_address=ip_address,
                management_ip=management_ip,
                management_port=management_port,
                nas_ip=nas_ip or None,
                description=description,
                pop_site_id=pop_site_id,
                rack_position=rack_position,
                status=status,
                supported_connection_types=supported_types,
                default_connection_type=default_connection_type,
                ssh_username=ssh_username,
                ssh_password=ssh_password,
                ssh_key=ssh_key,
                api_url=api_url,
                api_username=api_username,
                api_password=api_password,
                api_token=api_token,
                snmp_community=snmp_community,
                snmp_version=snmp_version,
                snmp_port=snmp_port,
                backup_enabled=bool(backup_enabled),
                backup_method=backup_method,
                backup_schedule=backup_schedule,
                shared_secret=shared_secret,
                coa_port=coa_port,
                firmware_version=firmware_version,
                serial_number=serial_number,
                notes=notes,
                tags=tags,
                is_active=bool(is_active),
            )
    except Exception as exc:
        return None, [str(exc)]
    return payload, []


def build_provisioning_template_payload(
    *,
    form: dict[str, Any],
    for_update: bool = False,
) -> tuple[ProvisioningTemplateCreate | ProvisioningTemplateUpdate | None, list[str]]:
    """Validate web template form values and build create/update payload."""
    errors: list[str] = []
    placeholder_list = None
    placeholders = form.get("placeholders")
    if placeholders:
        try:
            placeholder_list = json.loads(str(placeholders))
        except json.JSONDecodeError:
            errors.append("Invalid placeholders JSON")
    if errors:
        return None, errors

    try:
        name = cast(str | None, form.get("name"))
        vendor = NasVendor(str(form.get("vendor")))
        action = ProvisioningAction(str(form.get("action")))
        connection_type = (
            ConnectionType(str(form["connection_type"]))
            if form.get("connection_type")
            else None
        )
        template_content = cast(str | None, form.get("template_content"))
        description = cast(str | None, form.get("description") or None)
        is_active = cast(bool | None, form.get("is_active"))
        if for_update:
            payload: ProvisioningTemplateCreate | ProvisioningTemplateUpdate = (
                ProvisioningTemplateUpdate(
                    name=name,
                    vendor=vendor,
                    action=action,
                    connection_type=connection_type,
                    template_content=template_content,
                    description=description,
                    placeholders=placeholder_list,
                    is_active=is_active,
                )
            )
        else:
            payload = ProvisioningTemplateCreate(
                name=cast(str, name),
                vendor=vendor,
                action=action,
                connection_type=cast(ConnectionType, connection_type),
                template_content=cast(str, template_content),
                description=description,
                placeholders=placeholder_list,
                is_active=bool(is_active),
            )
    except Exception as exc:
        return None, [str(exc)]
    return payload, []


def create_provisioning_template_with_metadata(
    db: Session,
    *,
    payload: ProvisioningTemplateCreate,
) -> tuple[ProvisioningTemplate, dict[str, Any]]:
    """Create template and return audit metadata."""
    from app.services.nas.templates import ProvisioningTemplates

    template = ProvisioningTemplates.create(db, payload)
    return template, {"name": template.name}


def update_provisioning_template_with_metadata(
    db: Session,
    *,
    template_id: str,
    payload: ProvisioningTemplateUpdate,
) -> tuple[ProvisioningTemplate, dict[str, Any] | None]:
    """Update template and return audit metadata with field diffs."""
    from app.services.nas.templates import ProvisioningTemplates

    template = ProvisioningTemplates.get(db, template_id)
    before_snapshot = model_to_dict(template, exclude=TEMPLATE_AUDIT_EXCLUDE_FIELDS)
    updated_template = ProvisioningTemplates.update(db, template_id, payload)
    after_snapshot = model_to_dict(
        updated_template, exclude=TEMPLATE_AUDIT_EXCLUDE_FIELDS
    )
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    return updated_template, metadata


def build_nas_dashboard_data(
    db: Session,
    *,
    vendor: str | None,
    nas_type: str | None,
    status: str | None,
    pop_site_id: str | None,
    partner_org_id: str | None,
    online_status: str | None,
    search: str | None,
    refresh: str | None,
    page: int,
    limit: int = 25,
) -> dict[str, Any]:
    """Build NAS dashboard page datasets, filters, and pagination."""
    from app.services.nas.devices import NasDevices

    vendor_filter_value = nas_type or vendor
    vendor_filter = NasVendor(vendor_filter_value) if vendor_filter_value else None
    status_filter = NasDeviceStatus(status) if status else None
    search_filter = search if search else None
    pop_site_uuid = None
    if pop_site_id:
        try:
            pop_site_uuid = UUID(pop_site_id)
        except ValueError:
            pop_site_uuid = None

    devices_all = NasDevices.list(
        db=db,
        limit=1000,
        offset=0,
        order_by="name",
        order_dir="asc",
        vendor=vendor_filter,
        status=status_filter,
        pop_site_id=pop_site_uuid,
        search=search_filter,
    )
    if partner_org_id:
        devices_all = [
            device
            for device in devices_all
            if f"partner_org:{partner_org_id}"
            in [str(tag) for tag in (device.tags or [])]
        ]

    ping_statuses_all = {
        str(device.id): get_cached_ping_status(device) for device in devices_all
    }
    if online_status == "online":
        devices_all = [
            d
            for d in devices_all
            if ping_statuses_all.get(str(d.id), {}).get("state") == "reachable"
        ]
    elif online_status == "offline":
        devices_all = [
            d
            for d in devices_all
            if ping_statuses_all.get(str(d.id), {}).get("state") != "reachable"
        ]

    total = len(devices_all)
    offset = (page - 1) * limit
    devices = devices_all[offset : offset + limit]
    total_pages = (total + limit - 1) // limit

    linked_by_nas = _resolve_linked_monitoring_devices(db, devices)
    force_refresh = str(refresh or "").strip().lower() in {"1", "true", "yes", "on"}
    if linked_by_nas:
        try:
            ping_interval_seconds = int(
                str(
                    resolve_value(
                        db,
                        SettingDomain.network_monitoring,
                        "core_device_ping_interval_seconds",
                    )
                    or 120
                )
            )
        except (TypeError, ValueError):
            ping_interval_seconds = 120
        try:
            snmp_interval_seconds = int(
                str(
                    resolve_value(
                        db,
                        SettingDomain.network_monitoring,
                        "core_device_snmp_walk_interval_seconds",
                    )
                    or 300
                )
            )
        except (TypeError, ValueError):
            snmp_interval_seconds = 300
        from app.services import web_network_core_runtime as core_runtime_service

        core_runtime_service.refresh_stale_devices_health(
            db,
            list(linked_by_nas.values()),
            ping_interval_seconds=ping_interval_seconds,
            snmp_interval_seconds=snmp_interval_seconds,
            include_snmp=True,
            force=force_refresh,
        )
        # Re-read after refresh to ensure UI reflects current state.
        linked_by_nas = _resolve_linked_monitoring_devices(db, devices)

    runtime_statuses: dict[str, str] = {}
    runtime_last_seen: dict[str, datetime | None] = {}
    ping_statuses = {
        str(device.id): ping_statuses_all.get(
            str(device.id), {"state": "unknown", "label": "No host"}
        )
        for device in devices
    }
    for device in devices:
        nas_id = str(device.id)
        linked = linked_by_nas.get(nas_id)
        if not linked:
            continue
        runtime_statuses[nas_id] = linked.status.value if linked.status else "unknown"
        seen_at = linked.last_snmp_at or linked.last_ping_at
        runtime_last_seen[nas_id] = seen_at
        if linked.last_ping_ok is True:
            label = "Reachable"
            if linked.last_ping_at:
                label = f"Seen {linked.last_ping_at.strftime('%H:%M:%S')}"
            ping_statuses[nas_id] = {"state": "reachable", "label": label}
        elif linked.last_ping_ok is False:
            ping_statuses[nas_id] = {"state": "unreachable", "label": "Unreachable"}

    return {
        "devices": devices,
        "ping_statuses": ping_statuses,
        "runtime_statuses": runtime_statuses,
        "runtime_last_seen": runtime_last_seen,
        "stats": {
            "by_vendor": NasDevices.count_by_vendor(db),
            "by_status": NasDevices.count_by_status(db),
        },
        "pagination": {
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
        "filters": {
            "vendor": vendor,
            "nas_type": nas_type,
            "status": status,
            "pop_site_id": pop_site_id,
            "partner_org_id": partner_org_id,
            "online_status": online_status,
            "search": search,
            "refresh": refresh,
        },
    }


def _resolve_linked_monitoring_devices(
    db: Session,
    devices: list[NasDevice],
) -> dict[str, NetworkDevice]:
    """Map NAS device id -> linked monitoring device for runtime status display."""
    if not devices:
        return {}
    mapping: dict[str, NetworkDevice] = {}
    ids: list[UUID] = [
        cast(UUID, d.network_device_id)
        for d in devices
        if d.network_device_id is not None
    ]
    if ids:
        rows = (
            db.execute(select(NetworkDevice).where(NetworkDevice.id.in_(ids)))
            .scalars()
            .all()
        )
        by_id = {str(row.id): row for row in rows}
        for d in devices:
            if d.network_device_id and str(d.network_device_id) in by_id:
                mapping[str(d.id)] = by_id[str(d.network_device_id)]

    unresolved = [d for d in devices if str(d.id) not in mapping]
    if not unresolved:
        return mapping
    mgmt_hosts = {
        str(d.management_ip or d.ip_address or "").strip()
        for d in unresolved
        if (d.management_ip or d.ip_address)
    }
    mgmt_hosts.discard("")
    if not mgmt_hosts:
        return mapping
    rows = (
        db.execute(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip.in_(list(mgmt_hosts)))
        )
        .scalars()
        .all()
    )
    by_host = {str(row.mgmt_ip): row for row in rows if row.mgmt_ip}
    for d in unresolved:
        host = str(d.management_ip or d.ip_address or "").strip()
        if host and host in by_host:
            mapping[str(d.id)] = by_host[host]
    return mapping


def build_nas_device_detail_data(
    db: Session,
    *,
    device_id: str,
    tab: str,
    api_test_status: str | None,
    api_test_message: str | None,
    rule_status: str | None,
    rule_message: str | None,
    build_activities_fn,
) -> dict[str, Any]:
    """Build NAS device detail page payload."""
    from app.services.nas.backups import NasConfigBackups
    from app.services.nas.connection_rules import NasConnectionRules
    from app.services.nas.devices import NasDevices
    from app.services.nas.logs import ProvisioningLogs

    device = NasDevices.get(db, device_id)
    mikrotik_status = extract_mikrotik_status(device.tags)
    linked_device = _resolve_linked_monitoring_device(db, device)
    metrics_by_type = _latest_monitoring_metrics(db, linked_device)

    resolved_health_status = _resolve_nas_health_status(device, linked_device)
    resolved_last_health_check_at = (
        device.last_health_check_at
        or (linked_device.last_health_check_at if linked_device else None)
        or (linked_device.last_snmp_at if linked_device else None)
        or (linked_device.last_ping_at if linked_device else None)
    )
    resolved_last_seen_at = (
        device.last_seen_at
        or (linked_device.last_snmp_at if linked_device else None)
        or (linked_device.last_ping_at if linked_device else None)
    )
    resolved_location = device.rack_position or pop_site_label(device)
    resolved_model = (
        device.model
        or (linked_device.model if linked_device else None)
        or mikrotik_status.get("board_name")
    )
    resolved_firmware_version = device.firmware_version or mikrotik_status.get(
        "routeros_version"
    )
    resolved_serial_number = (
        device.serial_number
        or (linked_device.serial_number if linked_device else None)
        or mikrotik_status.get("serial_number")
    )
    resolved_current_subscriber_count = (
        linked_device.current_subscriber_count
        if linked_device and linked_device.current_subscriber_count is not None
        else device.current_subscriber_count
    )

    if not mikrotik_status.get("cpu_usage") and metrics_by_type.get("cpu"):
        mikrotik_status["cpu_usage"] = str(metrics_by_type["cpu"]["value"])
    if not mikrotik_status.get("last_status_check"):
        last_metric_at = metrics_by_type.get("latest_recorded_at")
        mikrotik_status["last_status_check"] = (
            str(last_metric_at) if last_metric_at else None
        )

    recent_backups = NasConfigBackups.list(
        db,
        nas_device_id=UUID(device_id),
        limit=10,
        offset=0,
    )
    recent_logs = ProvisioningLogs.list(
        db,
        nas_device_id=UUID(device_id),
        limit=10,
        offset=0,
    )
    connection_logs = _recent_nas_connection_auth_logs(
        db, nas_device_id=UUID(device_id), limit=50
    )
    activities = build_activities_fn(db, "nas_device", device_id, limit=10)
    connection_rules = NasConnectionRules.list(
        db, nas_device_id=device_id, is_active=None
    )
    if tab not in {
        "information",
        "connection-rules",
        "vendor-specific",
        "device-log",
        "map",
    }:
        tab = "information"

    return {
        "device": device,
        "backups": recent_backups,
        "logs": recent_logs,
        "connection_logs": connection_logs,
        "activities": activities,
        "ping_status": get_ping_status(device.ip_address or device.management_ip),
        "radius_pool_names": resolve_radius_pool_names(db, device),
        "partner_org_names": resolve_partner_org_names(db, device),
        "enhanced_fields": extract_enhanced_fields(device.tags),
        "connection_rules": connection_rules,
        "active_tab": tab,
        "api_test_status": api_test_status,
        "api_test_message": api_test_message,
        "rule_status": rule_status,
        "rule_message": rule_message,
        "mikrotik_status": mikrotik_status,
        "connection_types": [
            {"value": ct.value, "label": ct.value.upper()} for ct in ConnectionType
        ],
        "linked_device": linked_device,
        "metrics_by_type": metrics_by_type,
        "resolved_health_status": resolved_health_status,
        "resolved_last_health_check_at": resolved_last_health_check_at,
        "resolved_last_seen_at": resolved_last_seen_at,
        "resolved_location": resolved_location,
        "resolved_model": resolved_model,
        "resolved_firmware_version": resolved_firmware_version,
        "resolved_serial_number": resolved_serial_number,
        "resolved_current_subscriber_count": resolved_current_subscriber_count,
        "resolved_ssh_port": device.management_port,
    }


def _recent_nas_connection_auth_logs(
    db: Session,
    *,
    nas_device_id: UUID,
    limit: int = 50,
) -> list[ProvisioningLog]:
    """Return NAS connection auth attempts (server -> MikroTik) for detail tab."""
    rows = (
        db.execute(
            select(ProvisioningLog)
            .where(ProvisioningLog.nas_device_id == nas_device_id)
            .where(ProvisioningLog.command_sent.ilike("mikrotik_auth:%"))
            .order_by(ProvisioningLog.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows)


def _resolve_linked_monitoring_device(
    db: Session, device: NasDevice
) -> NetworkDevice | None:
    """Resolve monitoring device linked to a NAS device."""
    if device.network_device_id:
        linked = db.get(NetworkDevice, device.network_device_id)
        if linked:
            return linked
    host = (device.management_ip or device.ip_address or "").strip()
    if not host:
        return None
    return db.execute(
        select(NetworkDevice).where(NetworkDevice.mgmt_ip == host).limit(1)
    ).scalar_one_or_none()


def _latest_monitoring_metrics(
    db: Session, linked_device: NetworkDevice | None
) -> dict[str, Any]:
    """Return latest key monitoring metrics for a linked core device."""
    if not linked_device:
        return {}

    wanted = {
        MetricType.cpu: "cpu",
        MetricType.memory: "memory",
        MetricType.uptime: "uptime",
        MetricType.rx_bps: "rx_bps",
        MetricType.tx_bps: "tx_bps",
    }
    rows = db.execute(
        select(
            DeviceMetric.metric_type,
            DeviceMetric.value,
            DeviceMetric.unit,
            DeviceMetric.recorded_at,
        )
        .where(DeviceMetric.device_id == linked_device.id)
        .where(DeviceMetric.interface_id.is_(None))
        .where(DeviceMetric.metric_type.in_(list(wanted.keys())))
        .order_by(DeviceMetric.recorded_at.desc())
        .limit(250)
    ).all()

    latest: dict[str, Any] = {}
    latest_recorded_at = None
    for metric_type, value, unit, recorded_at in rows:
        key = wanted.get(metric_type)
        if not key or key in latest:
            continue
        latest[key] = {
            "value": value,
            "unit": unit,
            "recorded_at": recorded_at,
        }
        if latest_recorded_at is None:
            latest_recorded_at = recorded_at
    if latest_recorded_at is not None:
        latest["latest_recorded_at"] = latest_recorded_at
    return latest


def _resolve_nas_health_status(
    device: NasDevice, linked_device: NetworkDevice | None
) -> str:
    """Resolve best-available health status for NAS detail UI."""
    if device.health_status and getattr(device.health_status, "value", None):
        value = str(device.health_status.value)
        if value != "unknown":
            return value
    if (
        linked_device
        and linked_device.health_status
        and getattr(linked_device.health_status, "value", None)
    ):
        value = str(linked_device.health_status.value)
        if value != "unknown":
            return value
    if linked_device and linked_device.status:
        status_map = {
            DeviceStatus.online: "healthy",
            DeviceStatus.degraded: "degraded",
            DeviceStatus.offline: "unhealthy",
            DeviceStatus.maintenance: "unknown",
        }
        return status_map.get(linked_device.status, "unknown")
    return "unknown"


def build_nas_device_backups_page_data(
    db: Session,
    *,
    device_id: str,
    page: int,
    limit: int = 25,
) -> dict[str, Any]:
    """Build NAS device backups list page payload."""
    from app.services.nas.backups import NasConfigBackups
    from app.services.nas.devices import NasDevices

    device = NasDevices.get(db, device_id)
    offset = (page - 1) * limit
    backups = NasConfigBackups.list(
        db,
        nas_device_id=UUID(device_id),
        limit=limit,
        offset=offset,
    )
    total = NasConfigBackups.count(db, nas_device_id=UUID(device_id))
    total_pages = (total + limit - 1) // limit
    return {
        "device": device,
        "backups": backups,
        "pagination": {
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }


def build_nas_backup_detail_data(
    db: Session,
    *,
    backup_id: str,
    build_activities_fn,
) -> dict[str, Any]:
    """Build NAS backup detail page payload."""
    from app.services.nas.backups import NasConfigBackups
    from app.services.nas.devices import NasDevices

    backup = NasConfigBackups.get(db, backup_id)
    device = NasDevices.get(db, str(backup.nas_device_id))
    activities = build_activities_fn(db, "nas_backup", backup_id, limit=10)
    return {
        "backup": backup,
        "device": device,
        "activities": activities,
    }


def build_nas_backup_compare_data(
    db: Session,
    *,
    backup_id_1: str,
    backup_id_2: str,
) -> dict[str, Any]:
    """Build NAS backup compare page payload."""
    from app.services.nas.backups import NasConfigBackups
    from app.services.nas.devices import NasDevices

    diff = NasConfigBackups.compare(db, UUID(backup_id_1), UUID(backup_id_2))
    backup1 = NasConfigBackups.get(db, backup_id_1)
    backup2 = NasConfigBackups.get(db, backup_id_2)
    device = NasDevices.get(db, str(backup1.nas_device_id))
    return {
        "backup1": backup1,
        "backup2": backup2,
        "device": device,
        "diff": diff,
    }


def build_nas_templates_list_data(
    db: Session,
    *,
    vendor: str | None,
    action: str | None,
    page: int,
    limit: int = 25,
) -> dict[str, Any]:
    """Build NAS provisioning templates list page payload."""
    from app.services.nas.templates import ProvisioningTemplates

    offset = (page - 1) * limit
    vendor_filter = NasVendor(vendor) if vendor else None
    action_filter = ProvisioningAction(action) if action else None
    templates = ProvisioningTemplates.list(
        db=db,
        limit=limit,
        offset=offset,
        vendor=vendor_filter,
        action=action_filter,
    )
    total = ProvisioningTemplates.count(
        db=db,
        vendor=vendor_filter,
        action=action_filter,
    )
    total_pages = (total + limit - 1) // limit
    return {
        "templates": templates,
        "pagination": {
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
        "filters": {"vendor": vendor, "action": action},
    }


def build_nas_template_form_data(
    db: Session,
    *,
    template_id: str | None = None,
) -> dict[str, Any]:
    """Build NAS template form page payload."""
    from app.services.nas.templates import ProvisioningTemplates

    template = ProvisioningTemplates.get(db, template_id) if template_id else None
    return {
        "template": template,
        "errors": [],
    }


def build_nas_logs_list_data(
    db: Session,
    *,
    device_id: str | None,
    action: str | None,
    status: str | None,
    page: int,
    limit: int = 50,
) -> dict[str, Any]:
    """Build NAS provisioning logs list page payload."""
    from app.services.nas.devices import NasDevices
    from app.services.nas.logs import ProvisioningLogs

    offset = (page - 1) * limit
    device_filter = UUID(device_id) if device_id else None
    action_filter = ProvisioningAction(action) if action else None
    status_filter = ProvisioningLogStatus(status) if status else None
    logs = ProvisioningLogs.list(
        db=db,
        limit=limit,
        offset=offset,
        nas_device_id=device_filter,
        action=action_filter,
        status=status_filter,
    )
    total = ProvisioningLogs.count(
        db=db,
        nas_device_id=device_filter,
        action=action_filter,
        status=status_filter,
    )
    total_pages = (total + limit - 1) // limit
    devices = NasDevices.list(db, limit=500, offset=0)
    return {
        "logs": logs,
        "devices": devices,
        "pagination": {
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
        "filters": {
            "device_id": device_id,
            "action": action,
            "status": status,
        },
    }


def build_nas_log_detail_data(
    db: Session,
    *,
    log_id: str,
    build_activities_fn,
) -> dict[str, Any]:
    """Build NAS provisioning log detail page payload."""
    from app.services.nas.devices import NasDevices
    from app.services.nas.logs import ProvisioningLogs

    log = ProvisioningLogs.get(db, log_id)
    device = NasDevices.get(db, str(log.nas_device_id)) if log.nas_device_id else None
    activities = build_activities_fn(db, "nas_provision_log", log_id, limit=10)
    return {"log": log, "device": device, "activities": activities}


def trigger_backup_for_device(
    db: Session,
    *,
    device_id: str,
    triggered_by: str,
) -> dict[str, Any]:
    """Trigger NAS backup and queue failure notification when needed."""
    from app.services.nas.devices import NasDevices

    try:
        from app.services.nas import DeviceProvisioner

        backup = DeviceProvisioner.backup_config(db, UUID(device_id), triggered_by)
        return {"ok": True, "backup": backup, "error": None}
    except Exception as exc:
        error_message = str(exc)
        try:
            device = NasDevices.get(db, device_id)
            backup_alerts_service.queue_backup_failure_notification(
                db,
                device_kind="nas",
                device_name=device.name,
                device_ip=device.management_ip or device.ip_address,
                error_message=error_message,
                run_type="manual",
            )
            db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "backup": None, "error": error_message}


def get_ping_status(host: str | None) -> dict[str, object]:
    """Return lightweight ping status for list/detail badges."""
    if not host:
        return {"state": "unknown", "label": "No host"}
    success, latency_ms = ping_service.run_ping(host, timeout_seconds=4)
    if not success:
        return {"state": "unreachable", "label": "Unreachable"}
    if latency_ms is None:
        return {"state": "reachable", "label": "Reachable"}
    return {
        "state": "reachable",
        "label": f"Reachable {latency_ms:.1f} ms",
        "latency_ms": latency_ms,
    }


def get_cached_ping_status(
    device: NasDevice, *, stale_after_minutes: int = 10
) -> dict[str, object]:
    """Return cached reachability for list pages without active probing."""
    host = device.ip_address or device.management_ip
    if not host:
        return {"state": "unknown", "label": "No host"}
    if not device.last_seen_at:
        return {"state": "unknown", "label": "Unknown"}

    now = datetime.now(UTC)
    last_seen = device.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    age_seconds = max(int((now - last_seen).total_seconds()), 0)
    stale_after_seconds = max(1, stale_after_minutes) * 60
    age_minutes = max(1, age_seconds // 60)

    if age_seconds <= stale_after_seconds:
        return {"state": "reachable", "label": f"Seen {age_minutes}m ago"}
    return {"state": "unreachable", "label": f"Stale ({age_minutes}m)"}
