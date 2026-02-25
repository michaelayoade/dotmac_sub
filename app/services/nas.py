"""
NAS Device Management Service Layer

Provides CRUD operations and business logic for:
- NAS Device inventory management
- Configuration backup and restore
- Provisioning templates
- Device provisioning execution
"""
import hashlib
import ipaddress
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.catalog import (
    ConfigBackupMethod,
    ConnectionType,
    NasConfigBackup,
    NasConnectionRule,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningLogStatus,
    ProvisioningTemplate,
    RadiusProfile,
)
from app.models.network_monitoring import PopSite
from app.models.subscriber import Organization
from app.schemas.catalog import (
    NasConfigBackupCreate,
    NasDeviceCreate,
    NasDeviceUpdate,
    ProvisioningLogCreate,
    ProvisioningTemplateCreate,
    ProvisioningTemplateUpdate,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.credential_crypto import decrypt_credential, encrypt_nas_credentials
from app.services.response import ListResponseMixin
from app.services import backup_alerts as backup_alerts_service
from app.services.audit_helpers import diff_dicts, model_to_dict

_REDACT_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "ssh_key",
    "shared_secret",
}

RADIUS_REQUIRED_CONNECTION_TYPES = {
    ConnectionType.pppoe,
    ConnectionType.ipoe,
    ConnectionType.hotspot,
}
TEMPLATE_AUDIT_EXCLUDE_FIELDS = {"template_content"}


def list_pop_sites(db: Session, *, is_active: bool = True, limit: int = 500) -> list[PopSite]:
    """Return POP sites for NAS form/dropdown usage."""
    query = db.query(PopSite)
    if is_active:
        query = query.filter(PopSite.is_active.is_(True))
    return query.order_by(PopSite.name.asc()).limit(limit).all()


def get_pop_site(db: Session, pop_site_id: str | UUID) -> PopSite | None:
    """Return POP site by id or None."""
    try:
        site_uuid = coerce_uuid(pop_site_id)
    except (TypeError, ValueError):
        return None
    return db.get(PopSite, site_uuid)


def list_organizations(
    db: Session,
    *,
    ids: list[UUID] | None = None,
    limit: int = 500,
) -> list[Organization]:
    """Return organizations for NAS form and validation usage."""
    query = db.query(Organization)
    if ids:
        query = query.filter(Organization.id.in_(ids))
    return query.order_by(Organization.name.asc()).limit(limit).all()


def get_nas_form_options(db: Session) -> dict[str, object]:
    """Return dropdown/reference data for NAS web forms."""
    from app.services import network as network_service

    pop_sites = list_pop_sites(db, is_active=True, limit=500)
    ip_pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    organizations = list_organizations(db, limit=500)
    return {
        "pop_sites": pop_sites,
        "ip_pools": ip_pools,
        "organizations": organizations,
        "vendors": [{"value": v.value, "label": v.value.title()} for v in NasVendor],
        "statuses": [{"value": s.value, "label": s.value.title()} for s in NasDeviceStatus],
        "connection_types": [{"value": ct.value, "label": ct.value.upper()} for ct in ConnectionType],
        "backup_methods": [{"value": m.value, "label": m.value.upper()} for m in ConfigBackupMethod],
        "provisioning_actions": [
            {"value": a.value, "label": a.value.replace("_", " ").title()}
            for a in ProvisioningAction
        ],
    }


def validate_ipv4_address(value: str | None, field_label: str) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return f"{field_label} must be a valid IPv4 address."
    if ip.version != 4:
        return f"{field_label} must be an IPv4 address."
    return None


def prefixed_values_from_tags(tags: list[str] | None, prefix: str) -> list[str]:
    if not tags:
        return []
    values: list[str] = []
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(prefix):
            values.append(tag.split(":", 1)[1])
    return values


def prefixed_value_from_tags(tags: list[str] | None, prefix: str) -> str | None:
    values = prefixed_values_from_tags(tags, prefix)
    return values[0] if values else None


def radius_pool_ids_from_tags(tags: list[str] | None) -> list[str]:
    return prefixed_values_from_tags(tags, "radius_pool:")


def upsert_prefixed_tags(existing_tags: list[str] | None, prefix: str, values: list[str]) -> list[str]:
    base = [tag for tag in (existing_tags or []) if not tag.startswith(prefix)]
    return base + [f"{prefix}{value}" for value in values if value]


def merge_single_tag(existing_tags: list[str] | None, prefix: str, value: str | None) -> list[str] | None:
    merged = upsert_prefixed_tags(existing_tags, prefix, [value] if value else [])
    return merged or None


def merge_radius_pool_tags(existing_tags: list[str] | None, radius_pool_ids: list[str]) -> list[str] | None:
    merged = upsert_prefixed_tags(existing_tags, "radius_pool:", radius_pool_ids)
    return merged or None


def merge_partner_org_tags(existing_tags: list[str] | None, partner_org_ids: list[str]) -> list[str] | None:
    merged = upsert_prefixed_tags(existing_tags, "partner_org:", partner_org_ids)
    return merged or None


def extract_enhanced_fields(tags: list[str] | None) -> dict[str, str | list[str] | None]:
    return {
        "partner_org_ids": prefixed_values_from_tags(tags, "partner_org:"),
        "authorization_type": prefixed_value_from_tags(tags, "authorization_type:"),
        "accounting_type": prefixed_value_from_tags(tags, "accounting_type:"),
        "physical_address": prefixed_value_from_tags(tags, "physical_address:"),
        "latitude": prefixed_value_from_tags(tags, "latitude:"),
        "longitude": prefixed_value_from_tags(tags, "longitude:"),
        "mikrotik_api_enabled": prefixed_value_from_tags(tags, "mikrotik_api_enabled:"),
        "mikrotik_api_port": prefixed_value_from_tags(tags, "mikrotik_api_port:"),
        "shaper_enabled": prefixed_value_from_tags(tags, "shaper_enabled:"),
        "shaper_target": prefixed_value_from_tags(tags, "shaper_target:"),
        "shaping_type": prefixed_value_from_tags(tags, "shaping_type:"),
        "wireless_access_list": prefixed_value_from_tags(tags, "wireless_access_list:"),
        "disabled_customers_address_list": prefixed_value_from_tags(tags, "disabled_customers_address_list:"),
        "blocking_rules_enabled": prefixed_value_from_tags(tags, "blocking_rules_enabled:"),
    }


def extract_mikrotik_status(tags: list[str] | None) -> dict[str, str | None]:
    return {
        "platform": prefixed_value_from_tags(tags, "mikrotik_status_platform:"),
        "board_name": prefixed_value_from_tags(tags, "mikrotik_status_board_name:"),
        "routeros_version": prefixed_value_from_tags(tags, "mikrotik_status_routeros_version:"),
        "cpu_usage": prefixed_value_from_tags(tags, "mikrotik_status_cpu_usage:"),
        "ipv6_status": prefixed_value_from_tags(tags, "mikrotik_status_ipv6_status:"),
        "last_status_check": prefixed_value_from_tags(tags, "mikrotik_status_last_check:"),
    }


def resolve_radius_pool_names(db: Session, device: NasDevice) -> list[str]:
    from app.services import network as network_service

    ids = radius_pool_ids_from_tags(device.tags)
    if not ids:
        return []
    pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    return [str(pool.name) for pool in pools if str(pool.id) in ids]


def resolve_partner_org_names(db: Session, device: NasDevice) -> list[str]:
    ids = prefixed_values_from_tags(device.tags, "partner_org:")
    if not ids:
        return []
    valid_ids: list[UUID] = []
    for raw in ids:
        try:
            valid_ids.append(UUID(raw))
        except (ValueError, TypeError):
            continue
    if not valid_ids:
        return []
    orgs = list_organizations(db, ids=valid_ids, limit=500)
    return [str(org.name) for org in orgs]


def pop_site_label(device: NasDevice | None) -> str | None:
    if device and device.pop_site:
        label = str(device.pop_site.name)
        if device.pop_site.city:
            label = f"{label} ({str(device.pop_site.city)})"
        return label
    return None


def pop_site_label_by_id(db: Session, pop_site_id: str | None) -> str | None:
    if not pop_site_id:
        return None
    try:
        pop_site = get_pop_site(db, pop_site_id)
    except (ValueError, TypeError):
        pop_site = None
    if not pop_site:
        return None
    label = str(pop_site.name)
    if pop_site.city:
        label = f"{label} ({str(pop_site.city)})"
    return label


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

    ip_error = validate_ipv4_address(cast(str | None, form.get("ip_address")), "IP address")
    if ip_error:
        errors.append(ip_error)
    nas_ip = cast(str | None, form.get("nas_ip"))
    nas_ip_error = validate_ipv4_address(nas_ip, "NAS IP")
    if nas_ip_error:
        errors.append(nas_ip_error)
    if conn_types and any(ct in RADIUS_REQUIRED_CONNECTION_TYPES for ct in conn_types) and not nas_ip:
        errors.append("NAS IP is required when PPPoE, IPoE, or Hotspot authentication is enabled.")
    if str(form.get("authorization_type") or "").strip().lower() == "ppp_dhcp_radius" and not nas_ip:
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
    tags = merge_single_tag(tags, "authorization_type:", cast(str | None, form.get("authorization_type")))
    tags = merge_single_tag(tags, "accounting_type:", cast(str | None, form.get("accounting_type")))
    tags = merge_single_tag(tags, "physical_address:", cast(str | None, form.get("physical_address")))
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
    tags = merge_single_tag(tags, "shaper_target:", cast(str | None, form.get("shaper_target")))
    tags = merge_single_tag(tags, "shaping_type:", cast(str | None, form.get("shaping_type")))
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
        payload_kwargs = dict(
            name=form.get("name"),
            code=form.get("nas_identifier") or None,
            vendor=NasVendor(str(form.get("vendor"))),
            model=form.get("model") or None,
            ip_address=form.get("ip_address"),
            management_ip=form.get("ip_address"),
            management_port=form.get("ssh_port"),
            nas_ip=nas_ip or None,
            description=form.get("description") or None,
            pop_site_id=UUID(str(form["pop_site_id"])) if form.get("pop_site_id") else None,
            rack_position=form.get("location") or None,
            status=NasDeviceStatus(str(form.get("status"))),
            supported_connection_types=[ct.value for ct in conn_types] if conn_types else None,
            default_connection_type=ConnectionType(str(form["default_connection_type"]))
            if form.get("default_connection_type")
            else None,
            ssh_username=form.get("ssh_username") or None,
            ssh_password=form.get("ssh_password") or None,
            ssh_key=form.get("ssh_key") or None,
            api_url=form.get("api_url") or None,
            api_username=form.get("api_username") or None,
            api_password=form.get("api_password") or None,
            api_token=form.get("api_key") or None,
            snmp_community=form.get("snmp_community") or None,
            snmp_version=form.get("snmp_version") or None,
            snmp_port=form.get("snmp_port"),
            backup_enabled=form.get("backup_enabled"),
            backup_method=ConfigBackupMethod(str(form["backup_method"])) if form.get("backup_method") else None,
            backup_schedule=form.get("backup_schedule") or None,
            shared_secret=form.get("radius_secret") or None,
            coa_port=form.get("coa_port"),
            firmware_version=form.get("firmware_version") or None,
            serial_number=form.get("serial_number") or None,
            notes=form.get("notes") or None,
            tags=tags,
            is_active=form.get("is_active"),
        )
        payload = NasDeviceUpdate(**payload_kwargs) if for_update else NasDeviceCreate(**payload_kwargs)
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
        payload_kwargs = dict(
            name=form.get("name"),
            vendor=NasVendor(str(form.get("vendor"))),
            action=ProvisioningAction(str(form.get("action"))),
            connection_type=ConnectionType(str(form["connection_type"])) if form.get("connection_type") else None,
            template_content=form.get("template_content"),
            description=form.get("description") or None,
            placeholders=placeholder_list,
            is_active=form.get("is_active"),
        )
        payload = (
            ProvisioningTemplateUpdate(**payload_kwargs)
            if for_update
            else ProvisioningTemplateCreate(**payload_kwargs)
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
    template = ProvisioningTemplates.create(db, payload)
    return template, {"name": template.name}


def update_provisioning_template_with_metadata(
    db: Session,
    *,
    template_id: str,
    payload: ProvisioningTemplateUpdate,
) -> tuple[ProvisioningTemplate, dict[str, Any] | None]:
    """Update template and return audit metadata with field diffs."""
    template = ProvisioningTemplates.get(db, template_id)
    before_snapshot = model_to_dict(template, exclude=TEMPLATE_AUDIT_EXCLUDE_FIELDS)
    updated_template = ProvisioningTemplates.update(db, template_id, payload)
    after_snapshot = model_to_dict(updated_template, exclude=TEMPLATE_AUDIT_EXCLUDE_FIELDS)
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
    page: int,
    limit: int = 25,
) -> dict[str, Any]:
    """Build NAS dashboard page datasets, filters, and pagination."""
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
            if f"partner_org:{partner_org_id}" in [str(tag) for tag in (device.tags or [])]
        ]

    ping_statuses_all = {
        str(device.id): get_ping_status(device.ip_address or device.management_ip)
        for device in devices_all
    }
    if online_status == "online":
        devices_all = [d for d in devices_all if ping_statuses_all.get(str(d.id), {}).get("state") == "reachable"]
    elif online_status == "offline":
        devices_all = [d for d in devices_all if ping_statuses_all.get(str(d.id), {}).get("state") != "reachable"]

    total = len(devices_all)
    offset = (page - 1) * limit
    devices = devices_all[offset : offset + limit]
    total_pages = (total + limit - 1) // limit

    ping_statuses = {
        str(device.id): ping_statuses_all.get(str(device.id), {"state": "unknown", "label": "No host"})
        for device in devices
    }

    return {
        "devices": devices,
        "ping_statuses": ping_statuses,
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
        },
    }


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
    device = NasDevices.get(db, device_id)
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
    activities = build_activities_fn(db, "nas_device", device_id, limit=10)
    connection_rules = NasConnectionRules.list(db, nas_device_id=device_id, is_active=None)
    if tab not in {"information", "connection-rules", "vendor-specific", "device-log", "map"}:
        tab = "information"

    return {
        "device": device,
        "backups": recent_backups,
        "logs": recent_logs,
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
        "mikrotik_status": extract_mikrotik_status(device.tags),
        "connection_types": [{"value": ct.value, "label": ct.value.upper()} for ct in ConnectionType],
    }


def build_nas_device_backups_page_data(
    db: Session,
    *,
    device_id: str,
    page: int,
    limit: int = 25,
) -> dict[str, Any]:
    """Build NAS device backups list page payload."""
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
    try:
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


def _build_ping_command(host: str) -> list[str]:
    command = ["ping", "-c", "1", "-W", "2", host]
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 6:
            return ["ping", "-6", "-c", "1", "-W", "2", host]
    except ValueError:
        pass
    return command


def get_ping_status(host: str | None) -> dict[str, object]:
    """Return lightweight ping status for list/detail badges."""
    if not host:
        return {"state": "unknown", "label": "No host"}
    try:
        result = subprocess.run(
            _build_ping_command(host),
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        )
    except Exception:
        return {"state": "unreachable", "label": "Unreachable"}
    if result.returncode != 0:
        return {"state": "unreachable", "label": "Unreachable"}
    output = f"{result.stdout or ''} {result.stderr or ''}"
    latency_ms = None
    match = re.search(r"time[=<]\s*([0-9.]+)\s*ms", output)
    if match:
        try:
            latency_ms = float(match.group(1))
        except ValueError:
            latency_ms = None
    if latency_ms is None:
        return {"state": "reachable", "label": "Reachable"}
    return {"state": "reachable", "label": f"Reachable {latency_ms:.1f} ms", "latency_ms": latency_ms}


def get_mikrotik_api_status(device: NasDevice) -> dict[str, object]:
    """Test MikroTik API and return basic runtime status fields."""
    import requests

    if device.vendor != NasVendor.mikrotik:
        raise HTTPException(status_code=400, detail="Vendor-specific API status is only available for MikroTik devices.")
    if not device.api_url:
        raise HTTPException(status_code=400, detail="API URL is not configured.")

    auth = None
    headers: dict[str, str] = {}
    if device.api_token:
        headers["Authorization"] = f"Bearer {decrypt_credential(device.api_token)}"
    elif device.api_username and device.api_password:
        auth = (device.api_username, decrypt_credential(device.api_password))
    else:
        raise HTTPException(status_code=400, detail="API credentials are not configured.")

    base_url = device.api_url.rstrip("/")
    verify_tls = device.api_verify_tls if device.api_verify_tls is not None else False

    try:
        resource_resp = requests.get(
            f"{base_url}/rest/system/resource",
            auth=auth,
            headers=headers,
            timeout=10,
            verify=verify_tls,
        )
        resource_resp.raise_for_status()
        resource_data = resource_resp.json() if resource_resp.text else {}

        package_resp = requests.get(
            f"{base_url}/rest/system/package",
            auth=auth,
            headers=headers,
            timeout=10,
            verify=verify_tls,
        )
        package_resp.raise_for_status()
        package_data = package_resp.json() if package_resp.text else []
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"MikroTik API test failed: {exc}") from exc

    version = None
    if isinstance(package_data, list):
        for item in package_data:
            if isinstance(item, dict) and str(item.get("name")).lower() == "routeros":
                version = item.get("version")
                break
    if version is None and isinstance(resource_data, dict):
        version = resource_data.get("version")

    return {
        "platform": resource_data.get("platform") if isinstance(resource_data, dict) else None,
        "board_name": resource_data.get("board-name") if isinstance(resource_data, dict) else None,
        "routeros_version": version,
        "cpu_usage": resource_data.get("cpu-load") if isinstance(resource_data, dict) else None,
        "ipv6_status": "enabled" if resource_data.get("ipv6") else "unknown",
        "last_status_check": datetime.now(UTC),
    }


def _redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    def redact_value(value: Any) -> Any:
        if isinstance(value, dict):
            return _redact_sensitive(value)
        if isinstance(value, list):
            return [redact_value(item) for item in value]
        return value

    redacted: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key.lower() in _REDACT_KEYS:
            redacted[key] = "***redacted***"
        else:
            redacted[key] = redact_value(value)
    return redacted


# =============================================================================
# NAS DEVICE SERVICE
# =============================================================================

class NasDevices(ListResponseMixin):
    """Service class for NAS device CRUD operations."""

    ALLOWED_ORDER_COLUMNS = {
        "name": NasDevice.name,
        "vendor": NasDevice.vendor,
        "status": NasDevice.status,
        "created_at": NasDevice.created_at,
        "updated_at": NasDevice.updated_at,
    }

    @staticmethod
    def create(db: Session, payload: NasDeviceCreate) -> NasDevice:
        """Create a new NAS device."""
        data = payload.model_dump(exclude_unset=True)

        # Validate pop_site if provided
        if data.get("pop_site_id"):
            pop_site = db.get(PopSite, data["pop_site_id"])
            if not pop_site:
                raise HTTPException(status_code=404, detail="POP site not found")

        # Encrypt credential fields before storage
        data = encrypt_nas_credentials(data)

        device = NasDevice(**data)
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str | UUID) -> NasDevice:
        """Get a NAS device by ID."""
        device_id = coerce_uuid(device_id)
        device = cast(NasDevice | None, db.get(NasDevice, device_id))
        if not device:
            raise HTTPException(status_code=404, detail="NAS device not found")
        return device

    @staticmethod
    def get_by_code(db: Session, code: str) -> NasDevice | None:
        """Get a NAS device by its code."""
        return cast(
            NasDevice | None,
            db.execute(
            select(NasDevice).where(NasDevice.code == code)
            ).scalar_one_or_none(),
        )

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "name",
        order_dir: str = "asc",
        vendor: NasVendor | None = None,
        status: NasDeviceStatus | None = None,
        connection_type: ConnectionType | None = None,
        pop_site_id: UUID | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> list[NasDevice]:
        """List NAS devices with filtering and pagination."""
        query = select(NasDevice)

        if vendor:
            query = query.where(NasDevice.vendor == vendor)
        if status:
            query = query.where(NasDevice.status == status)
        if connection_type:
            query = query.where(
                NasDevice.supported_connection_types.contains([connection_type.value])
            )
        if pop_site_id:
            query = query.where(NasDevice.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.where(NasDevice.is_active.is_(True))
        else:
            query = query.where(NasDevice.is_active == is_active)
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (NasDevice.name.ilike(search_pattern))
                | (NasDevice.code.ilike(search_pattern))
                | (NasDevice.ip_address.ilike(search_pattern))
                | (NasDevice.management_ip.ilike(search_pattern))
            )

        query = apply_ordering(query, order_by, order_dir, NasDevices.ALLOWED_ORDER_COLUMNS)
        query = apply_pagination(query, limit, offset)

        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(db: Session, device_id: str | UUID, payload: NasDeviceUpdate) -> NasDevice:
        """Update a NAS device."""
        device = NasDevices.get(db, device_id)
        data = payload.model_dump(exclude_unset=True)

        # Validate pop_site if being changed
        if "pop_site_id" in data and data["pop_site_id"]:
            pop_site = db.get(PopSite, data["pop_site_id"])
            if not pop_site:
                raise HTTPException(status_code=404, detail="POP site not found")

        # Encrypt credential fields before storage
        data = encrypt_nas_credentials(data)

        for key, value in data.items():
            setattr(device, key, value)

        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str | UUID) -> None:
        """Delete a NAS device."""
        device = NasDevices.get(db, device_id)
        device.is_active = False
        device.status = NasDeviceStatus.decommissioned
        db.commit()

    @staticmethod
    def update_last_seen(db: Session, device_id: str | UUID) -> NasDevice:
        """Update the last_seen_at timestamp for a device."""
        device = NasDevices.get(db, device_id)
        device.last_seen_at = datetime.now(UTC)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def count(
        db: Session,
        *,
        vendor: NasVendor | None = None,
        status: NasDeviceStatus | None = None,
        connection_type: ConnectionType | None = None,
        pop_site_id: UUID | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> int:
        """Count NAS devices with filtering (same filters as list)."""
        query = select(func.count(NasDevice.id))

        if vendor:
            query = query.where(NasDevice.vendor == vendor)
        if status:
            query = query.where(NasDevice.status == status)
        if connection_type:
            query = query.where(
                NasDevice.supported_connection_types.contains([connection_type.value])
            )
        if pop_site_id:
            query = query.where(NasDevice.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.where(NasDevice.is_active.is_(True))
        else:
            query = query.where(NasDevice.is_active == is_active)
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (NasDevice.name.ilike(search_pattern))
                | (NasDevice.code.ilike(search_pattern))
                | (NasDevice.ip_address.ilike(search_pattern))
                | (NasDevice.management_ip.ilike(search_pattern))
            )

        return db.execute(query).scalar() or 0

    @staticmethod
    def count_by_vendor(db: Session) -> dict[str, int]:
        """Get count of devices grouped by vendor."""
        result = db.execute(
            select(NasDevice.vendor, func.count(NasDevice.id))
            .group_by(NasDevice.vendor)
        ).all()
        return {str(vendor.value): count for vendor, count in result}

    @staticmethod
    def count_by_status(db: Session) -> dict[str, int]:
        """Get count of devices grouped by status."""
        result = db.execute(
            select(NasDevice.status, func.count(NasDevice.id))
            .group_by(NasDevice.status)
        ).all()
        return {str(status.value): count for status, count in result}

    @staticmethod
    def get_stats(db: Session) -> dict:
        """Get combined NAS device statistics by vendor and status."""
        return {
            "by_vendor": NasDevices.count_by_vendor(db),
            "by_status": NasDevices.count_by_status(db),
        }


# =============================================================================
# NAS CONNECTION RULE SERVICE
# =============================================================================

class NasConnectionRules(ListResponseMixin):
    """Service class for per-device connection rules."""

    @staticmethod
    def get(db: Session, rule_id: str | UUID) -> NasConnectionRule:
        rule_id = coerce_uuid(rule_id)
        rule = db.get(NasConnectionRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Connection rule not found")
        return cast(NasConnectionRule, rule)

    @staticmethod
    def list(
        db: Session,
        *,
        nas_device_id: str | UUID,
        is_active: bool | None = None,
    ) -> list[NasConnectionRule]:
        device_id = coerce_uuid(nas_device_id)
        query = select(NasConnectionRule).where(NasConnectionRule.nas_device_id == device_id)
        if is_active is not None:
            query = query.where(NasConnectionRule.is_active == is_active)
        query = query.order_by(NasConnectionRule.priority.asc(), NasConnectionRule.name.asc())
        return list(db.execute(query).scalars().all())

    @staticmethod
    def create(
        db: Session,
        *,
        nas_device_id: str | UUID,
        name: str,
        connection_type: ConnectionType | str | None = None,
        ip_assignment_mode: str | None = None,
        rate_limit_profile: str | None = None,
        match_expression: str | None = None,
        priority: int = 100,
        is_active: bool = True,
        notes: str | None = None,
    ) -> NasConnectionRule:
        device = NasDevices.get(db, nas_device_id)
        rule_name = (name or "").strip()
        if not rule_name:
            raise HTTPException(status_code=400, detail="Rule name is required")

        normalized_connection_type = None
        if connection_type:
            normalized_connection_type = (
                connection_type
                if isinstance(connection_type, ConnectionType)
                else ConnectionType(connection_type)
            )

        rule = NasConnectionRule(
            nas_device_id=device.id,
            name=rule_name,
            connection_type=normalized_connection_type,
            ip_assignment_mode=(ip_assignment_mode or "").strip() or None,
            rate_limit_profile=(rate_limit_profile or "").strip() or None,
            match_expression=(match_expression or "").strip() or None,
            priority=priority,
            is_active=is_active,
            notes=(notes or "").strip() or None,
        )
        db.add(rule)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail="A connection rule with this name already exists for the selected device.",
            ) from exc
        db.refresh(rule)
        return rule

    @staticmethod
    def set_active(
        db: Session,
        *,
        rule_id: str | UUID,
        nas_device_id: str | UUID,
        is_active: bool,
    ) -> NasConnectionRule:
        rule = NasConnectionRules.get(db, rule_id)
        device_id = coerce_uuid(nas_device_id)
        if rule.nas_device_id != device_id:
            raise HTTPException(status_code=404, detail="Connection rule not found for NAS device")
        rule.is_active = is_active
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def delete(db: Session, *, rule_id: str | UUID, nas_device_id: str | UUID) -> None:
        rule = NasConnectionRules.get(db, rule_id)
        device_id = coerce_uuid(nas_device_id)
        if rule.nas_device_id != device_id:
            raise HTTPException(status_code=404, detail="Connection rule not found for NAS device")
        db.delete(rule)
        db.commit()


def create_connection_rule_for_device(
    db: Session,
    *,
    device_id: str,
    name: str,
    connection_type: str | None,
    ip_assignment_mode: str | None,
    rate_limit_profile: str | None,
    match_expression: str | None,
    priority: int,
    notes: str | None,
) -> str:
    NasConnectionRules.create(
        db,
        nas_device_id=device_id,
        name=name,
        connection_type=connection_type or None,
        ip_assignment_mode=ip_assignment_mode,
        rate_limit_profile=rate_limit_profile,
        match_expression=match_expression,
        priority=priority,
        notes=notes,
    )
    return "Connection rule created."


def toggle_connection_rule_for_device(
    db: Session,
    *,
    device_id: str,
    rule_id: str,
    is_active_raw: str,
) -> str:
    active = is_active_raw.strip().lower() in {"1", "true", "yes", "on"}
    NasConnectionRules.set_active(
        db,
        rule_id=rule_id,
        nas_device_id=device_id,
        is_active=active,
    )
    return "Connection rule enabled." if active else "Connection rule disabled."


def delete_connection_rule_for_device(
    db: Session,
    *,
    device_id: str,
    rule_id: str,
) -> str:
    NasConnectionRules.delete(db, rule_id=rule_id, nas_device_id=device_id)
    return "Connection rule deleted."


def refresh_mikrotik_status_for_device(db: Session, *, device_id: str) -> str:
    device = NasDevices.get(db, device_id)
    status = get_mikrotik_api_status(device)
    tags = device.tags
    tags = merge_single_tag(tags, "mikrotik_status_platform:", str(status.get("platform") or "-"))
    tags = merge_single_tag(tags, "mikrotik_status_board_name:", str(status.get("board_name") or "-"))
    tags = merge_single_tag(tags, "mikrotik_status_routeros_version:", str(status.get("routeros_version") or "-"))
    tags = merge_single_tag(tags, "mikrotik_status_cpu_usage:", str(status.get("cpu_usage") or "-"))
    tags = merge_single_tag(tags, "mikrotik_status_ipv6_status:", str(status.get("ipv6_status") or "-"))
    last_check = status.get("last_status_check")
    tags = merge_single_tag(tags, "mikrotik_status_last_check:", str(last_check) if last_check else "-")
    NasDevices.update(db, device_id, NasDeviceUpdate(tags=tags))
    return (
        f"Connected. Platform={status.get('platform') or '-'}, "
        f"Board={status.get('board_name') or '-'}, "
        f"RouterOS={status.get('routeros_version') or '-'}"
    )


# =============================================================================
# NAS CONFIG BACKUP SERVICE
# =============================================================================

class NasConfigBackups(ListResponseMixin):
    """Service class for NAS configuration backup operations."""

    @staticmethod
    def create(db: Session, payload: NasConfigBackupCreate) -> NasConfigBackup:
        """Create a new config backup."""
        # Verify device exists
        device = NasDevices.get(db, payload.nas_device_id)

        # Mark previous backups as not current (single atomic UPDATE).
        db.execute(
            update(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == device.id)
            .where(NasConfigBackup.is_current.is_(True))
            .values(is_current=False)
        )

        # Create new backup
        data = payload.model_dump(exclude_unset=True)
        config_content = data["config_content"]

        # Calculate hash and size
        config_hash = hashlib.sha256(config_content.encode()).hexdigest()
        config_size = len(config_content.encode())

        # Check if content changed from previous backup
        previous = db.execute(
            select(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == device.id)
            .order_by(NasConfigBackup.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        has_changes = previous is None or previous.config_hash != config_hash

        backup = NasConfigBackup(
            **data,
            config_hash=config_hash,
            config_size_bytes=config_size,
            has_changes=has_changes,
            is_current=True,
        )
        db.add(backup)

        # Update device last_backup_at
        device.last_backup_at = datetime.now(UTC)

        db.commit()
        db.refresh(backup)
        return backup

    @staticmethod
    def cleanup_retention(
        db: Session,
        *,
        keep_last: int = 10,
        keep_all_days: int = 7,
        keep_daily_days: int = 30,
        keep_weekly_days: int = 365,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Apply retention policy to NAS config backups."""
        now = now or datetime.now(UTC)
        keep_all_cutoff = now - timedelta(days=keep_all_days)
        keep_daily_cutoff = now - timedelta(days=keep_daily_days)
        keep_weekly_cutoff = now - timedelta(days=keep_weekly_days)

        device_ids = db.execute(select(NasConfigBackup.nas_device_id).distinct()).scalars().all()
        deleted = 0
        kept = 0

        for device_id in device_ids:
            backups = db.execute(
                select(NasConfigBackup)
                .where(NasConfigBackup.nas_device_id == device_id)
                .order_by(NasConfigBackup.created_at.desc())
            ).scalars().all()

            keep_ids: set[UUID] = set()
            daily_kept: set[str] = set()
            weekly_kept: set[str] = set()

            for backup in backups:
                if backup.keep_forever:
                    keep_ids.add(backup.id)

            for backup in backups[:keep_last]:
                keep_ids.add(backup.id)

            for backup in backups:
                if backup.id in keep_ids:
                    continue
                created_at = backup.created_at or now
                if created_at >= keep_all_cutoff:
                    keep_ids.add(backup.id)
                    continue
                if created_at >= keep_daily_cutoff:
                    day_key = created_at.date().isoformat()
                    if day_key not in daily_kept:
                        daily_kept.add(day_key)
                        keep_ids.add(backup.id)
                    continue
                if created_at >= keep_weekly_cutoff:
                    week_key = f"{created_at.isocalendar().year}-W{created_at.isocalendar().week}"
                    if week_key not in weekly_kept:
                        weekly_kept.add(week_key)
                        keep_ids.add(backup.id)

            for backup in backups:
                if backup.id in keep_ids:
                    kept += 1
                    continue
                db.delete(backup)
                deleted += 1

        db.commit()
        return {"deleted": deleted, "kept": kept}

    @staticmethod
    def get(db: Session, backup_id: str | UUID) -> NasConfigBackup:
        """Get a config backup by ID."""
        backup_id = coerce_uuid(backup_id)
        backup = cast(NasConfigBackup | None, db.get(NasConfigBackup, backup_id))
        if not backup:
            raise HTTPException(status_code=404, detail="Config backup not found")
        return backup

    @staticmethod
    def list(
        db: Session,
        *,
        nas_device_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        is_current: bool | None = None,
        has_changes: bool | None = None,
    ) -> list[NasConfigBackup]:
        """List config backups with filtering."""
        query = select(NasConfigBackup).order_by(NasConfigBackup.created_at.desc())

        if nas_device_id:
            query = query.where(NasConfigBackup.nas_device_id == nas_device_id)
        if is_current is not None:
            query = query.where(NasConfigBackup.is_current == is_current)
        if has_changes is not None:
            query = query.where(NasConfigBackup.has_changes == has_changes)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        nas_device_id: UUID | None = None,
        is_current: bool | None = None,
        has_changes: bool | None = None,
    ) -> int:
        """Count config backups with filtering (same filters as list)."""
        query = select(func.count(NasConfigBackup.id))

        if nas_device_id:
            query = query.where(NasConfigBackup.nas_device_id == nas_device_id)
        if is_current is not None:
            query = query.where(NasConfigBackup.is_current == is_current)
        if has_changes is not None:
            query = query.where(NasConfigBackup.has_changes == has_changes)

        return db.execute(query).scalar() or 0

    @staticmethod
    def get_current(db: Session, nas_device_id: UUID) -> NasConfigBackup | None:
        """Get the current (latest) backup for a device."""
        return cast(
            NasConfigBackup | None,
            db.execute(
                select(NasConfigBackup)
                .where(NasConfigBackup.nas_device_id == nas_device_id)
                .where(NasConfigBackup.is_current == True)
            ).scalar_one_or_none(),
        )

    @staticmethod
    def compare(db: Session, backup_id_1: UUID, backup_id_2: UUID) -> dict:
        """Compare two config backups and return diff info."""
        backup1 = NasConfigBackups.get(db, backup_id_1)
        backup2 = NasConfigBackups.get(db, backup_id_2)

        lines1 = backup1.config_content.splitlines()
        lines2 = backup2.config_content.splitlines()

        # Simple line-by-line comparison
        added = []
        removed = []
        set1 = set(lines1)
        set2 = set(lines2)

        for line in lines2:
            if line not in set1 and line.strip():
                added.append(line)
        for line in lines1:
            if line not in set2 and line.strip():
                removed.append(line)

        return {
            "backup_1": {"id": str(backup1.id), "created_at": backup1.created_at},
            "backup_2": {"id": str(backup2.id), "created_at": backup2.created_at},
            "lines_added": len(added),
            "lines_removed": len(removed),
            "added": added[:100],  # Limit to first 100
            "removed": removed[:100],
        }


# =============================================================================
# PROVISIONING TEMPLATE SERVICE
# =============================================================================

class ProvisioningTemplates(ListResponseMixin):
    """Service class for provisioning template operations."""

    @staticmethod
    def create(db: Session, payload: ProvisioningTemplateCreate) -> ProvisioningTemplate:
        """Create a new provisioning template."""
        data = payload.model_dump(exclude_unset=True)

        # Extract placeholders from template content if not provided
        if not data.get("placeholders"):
            content = data.get("template_content", "")
            # Find all {{placeholder}} patterns
            placeholders = list(set(re.findall(r"\{\{(\w+)\}\}", content)))
            data["placeholders"] = placeholders

        template = ProvisioningTemplate(**data)
        db.add(template)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def get(db: Session, template_id: str | UUID) -> ProvisioningTemplate:
        """Get a provisioning template by ID."""
        template_id = coerce_uuid(template_id)
        template = cast(
            ProvisioningTemplate | None, db.get(ProvisioningTemplate, template_id)
        )
        if not template:
            raise HTTPException(status_code=404, detail="Provisioning template not found")
        return template

    @staticmethod
    def get_by_code(db: Session, code: str) -> ProvisioningTemplate | None:
        """Get a template by its code."""
        return cast(
            ProvisioningTemplate | None,
            db.execute(
                select(ProvisioningTemplate).where(ProvisioningTemplate.code == code)
            ).scalar_one_or_none(),
        )

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        action: ProvisioningAction | None = None,
        is_active: bool | None = None,
    ) -> list[ProvisioningTemplate]:
        """List provisioning templates with filtering."""
        query = select(ProvisioningTemplate).order_by(ProvisioningTemplate.name)

        if vendor:
            query = query.where(ProvisioningTemplate.vendor == vendor)
        if connection_type:
            query = query.where(ProvisioningTemplate.connection_type == connection_type)
        if action:
            query = query.where(ProvisioningTemplate.action == action)
        if is_active is not None:
            query = query.where(ProvisioningTemplate.is_active == is_active)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        action: ProvisioningAction | None = None,
        is_active: bool | None = None,
    ) -> int:
        """Count provisioning templates with filtering (same filters as list)."""
        query = select(func.count(ProvisioningTemplate.id))

        if vendor:
            query = query.where(ProvisioningTemplate.vendor == vendor)
        if connection_type:
            query = query.where(ProvisioningTemplate.connection_type == connection_type)
        if action:
            query = query.where(ProvisioningTemplate.action == action)
        if is_active is not None:
            query = query.where(ProvisioningTemplate.is_active == is_active)

        return db.execute(query).scalar() or 0

    @staticmethod
    def find_template(
        db: Session,
        vendor: NasVendor,
        connection_type: ConnectionType,
        action: ProvisioningAction,
    ) -> ProvisioningTemplate | None:
        """Find the best matching template for given criteria."""
        # First try exact match
        template = cast(
            ProvisioningTemplate | None,
            db.execute(
                select(ProvisioningTemplate)
                .where(ProvisioningTemplate.vendor == vendor)
                .where(ProvisioningTemplate.connection_type == connection_type)
                .where(ProvisioningTemplate.action == action)
                .where(ProvisioningTemplate.is_active == True)
                .order_by(ProvisioningTemplate.is_default.desc())
                .limit(1)
            ).scalar_one_or_none(),
        )

        if template:
            return template

        # Fall back to "other" vendor with same connection type and action
        return cast(
            ProvisioningTemplate | None,
            db.execute(
                select(ProvisioningTemplate)
                .where(ProvisioningTemplate.vendor == NasVendor.other)
                .where(ProvisioningTemplate.connection_type == connection_type)
                .where(ProvisioningTemplate.action == action)
                .where(ProvisioningTemplate.is_active == True)
                .limit(1)
            ).scalar_one_or_none(),
        )

    @staticmethod
    def update(
        db: Session, template_id: str | UUID, payload: ProvisioningTemplateUpdate
    ) -> ProvisioningTemplate:
        """Update a provisioning template."""
        template = ProvisioningTemplates.get(db, template_id)
        data = payload.model_dump(exclude_unset=True)

        # Re-extract placeholders if content changed
        if "template_content" in data and not data.get("placeholders"):
            content = data["template_content"]
            placeholders = list(set(re.findall(r"\{\{(\w+)\}\}", content)))
            data["placeholders"] = placeholders

        for key, value in data.items():
            setattr(template, key, value)

        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def delete(db: Session, template_id: str | UUID) -> None:
        """Delete a provisioning template."""
        template = ProvisioningTemplates.get(db, template_id)
        db.delete(template)
        db.commit()

    @staticmethod
    def render(template: ProvisioningTemplate, variables: dict[str, Any]) -> str:
        """Render a template with given variables."""
        content = str(template.template_content or "")
        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", str(value))
        return content


# =============================================================================
# PROVISIONING LOG SERVICE
# =============================================================================

class ProvisioningLogs(ListResponseMixin):
    """Service class for provisioning log operations."""

    @staticmethod
    def create(db: Session, payload: ProvisioningLogCreate) -> ProvisioningLog:
        """Create a new provisioning log entry."""
        data = payload.model_dump(exclude_unset=True)
        log = ProvisioningLog(**data)
        db.add(log)
        db.commit()
        db.refresh(log)
        return log

    @staticmethod
    def get(db: Session, log_id: str | UUID) -> ProvisioningLog:
        """Get a provisioning log by ID."""
        log_id = coerce_uuid(log_id)
        log = cast(ProvisioningLog | None, db.get(ProvisioningLog, log_id))
        if not log:
            raise HTTPException(status_code=404, detail="Provisioning log not found")
        return log

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        nas_device_id: UUID | None = None,
        subscriber_id: UUID | None = None,
        action: ProvisioningAction | None = None,
        status: ProvisioningLogStatus | None = None,
    ) -> list[ProvisioningLog]:
        """List provisioning logs with filtering."""
        query = select(ProvisioningLog).order_by(ProvisioningLog.created_at.desc())

        if nas_device_id:
            query = query.where(ProvisioningLog.nas_device_id == nas_device_id)
        if subscriber_id:
            query = query.where(ProvisioningLog.subscriber_id == subscriber_id)
        if action:
            query = query.where(ProvisioningLog.action == action)
        if status:
            query = query.where(ProvisioningLog.status == status)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        nas_device_id: UUID | None = None,
        subscriber_id: UUID | None = None,
        action: ProvisioningAction | None = None,
        status: ProvisioningLogStatus | None = None,
    ) -> int:
        """Count provisioning logs with filtering (same filters as list)."""
        query = select(func.count(ProvisioningLog.id))

        if nas_device_id:
            query = query.where(ProvisioningLog.nas_device_id == nas_device_id)
        if subscriber_id:
            query = query.where(ProvisioningLog.subscriber_id == subscriber_id)
        if action:
            query = query.where(ProvisioningLog.action == action)
        if status:
            query = query.where(ProvisioningLog.status == status)

        return db.execute(query).scalar() or 0

    @staticmethod
    def update_status(
        db: Session,
        log_id: UUID,
        status: ProvisioningLogStatus,
        response: str | None = None,
        error: str | None = None,
        execution_time_ms: int | None = None,
    ) -> ProvisioningLog:
        """Update the status of a provisioning log."""
        log = ProvisioningLogs.get(db, log_id)
        log.status = status
        if response:
            log.response_received = response
        if error:
            log.error_message = error
        if execution_time_ms:
            log.execution_time_ms = execution_time_ms
        db.commit()
        db.refresh(log)
        return log


# =============================================================================
# RADIUS PROFILE SERVICE (Enhanced)
# =============================================================================

class RadiusProfiles(ListResponseMixin):
    """Service class for RADIUS profile operations."""

    @staticmethod
    def get(db: Session, profile_id: str | UUID) -> RadiusProfile:
        """Get a RADIUS profile by ID."""
        profile_id = coerce_uuid(profile_id)
        profile = cast(RadiusProfile | None, db.get(RadiusProfile, profile_id))
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        return profile

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        is_active: bool | None = None,
    ) -> list[RadiusProfile]:
        """List RADIUS profiles with filtering."""
        query = select(RadiusProfile).order_by(RadiusProfile.name)

        if vendor:
            query = query.where(RadiusProfile.vendor == vendor)
        if connection_type:
            query = query.where(RadiusProfile.connection_type == connection_type)
        if is_active is not None:
            query = query.where(RadiusProfile.is_active == is_active)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def generate_mikrotik_rate_limit(profile: RadiusProfile) -> str:
        """Generate MikroTik rate-limit string from profile settings."""
        if profile.mikrotik_rate_limit:
            return str(profile.mikrotik_rate_limit)

        if not profile.download_speed or not profile.upload_speed:
            return ""

        # Convert Kbps to format: rx/tx (download/upload in MikroTik terms)
        # MikroTik format: rx-rate[/tx-rate] [rx-burst-rate[/tx-burst-rate] [rx-burst-threshold[/tx-burst-threshold] [rx-burst-time[/tx-burst-time]]]]
        download_k = f"{profile.download_speed}k"
        upload_k = f"{profile.upload_speed}k"

        rate_limit = f"{download_k}/{upload_k}"

        if profile.burst_download and profile.burst_upload:
            burst_down = f"{profile.burst_download}k"
            burst_up = f"{profile.burst_upload}k"
            rate_limit += f" {burst_down}/{burst_up}"

            if profile.burst_threshold:
                threshold = f"{profile.burst_threshold}k"
                rate_limit += f" {threshold}/{threshold}"

                if profile.burst_time:
                    rate_limit += f" {profile.burst_time}s/{profile.burst_time}s"

        return rate_limit


# =============================================================================
# DEVICE PROVISIONER - Execute commands on NAS devices
# =============================================================================

class DeviceProvisioner:
    """
    Execute provisioning commands on NAS devices.

    Supports multiple execution methods:
    - SSH: Direct SSH command execution
    - API: REST API calls (MikroTik REST API, Huawei NCE, etc.)
    - RADIUS CoA: Change of Authorization packets
    """

    @staticmethod
    def provision_user(
        db: Session,
        nas_device_id: UUID,
        action: ProvisioningAction,
        variables: dict[str, Any],
        triggered_by: str = "system",
    ) -> ProvisioningLog:
        """
        Execute a provisioning action on a NAS device.

        Args:
            db: Database session
            nas_device_id: Target NAS device ID
            action: The provisioning action to execute
            variables: Variables to substitute in the template
            triggered_by: Who triggered this action

        Returns:
            ProvisioningLog with execution results
        """
        import time

        device = NasDevices.get(db, nas_device_id)

        # Determine connection type
        connection_type = device.default_connection_type or ConnectionType.pppoe

        # Find appropriate template
        template = ProvisioningTemplates.find_template(
            db, device.vendor, connection_type, action
        )

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"No provisioning template found for {device.vendor.value}/{connection_type.value}/{action.value}",
            )

        # Render the command
        command = ProvisioningTemplates.render(template, variables)

        # Create log entry
        log = ProvisioningLogs.create(
            db,
            ProvisioningLogCreate(
                nas_device_id=device.id,
                subscriber_id=variables.get("subscriber_id"),
                template_id=template.id,
                action=action,
                command_sent=command,
                status=ProvisioningLogStatus.running,
                triggered_by=triggered_by,
                request_data=_redact_sensitive(variables),
            ),
        )

        # Execute the command
        start_time = time.time()
        try:
            execution_method = template.execution_method or "ssh"

            if execution_method == "ssh":
                response = DeviceProvisioner._execute_ssh(device, command)
            elif execution_method == "api":
                response = DeviceProvisioner._execute_api(device, command, variables)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported execution method: {execution_method}",
                )

            execution_time = int((time.time() - start_time) * 1000)

            # Update log with success
            ProvisioningLogs.update_status(
                db,
                log.id,
                ProvisioningLogStatus.success,
                response=response,
                execution_time_ms=execution_time,
            )

            # Handle queue mapping for bandwidth monitoring
            DeviceProvisioner._handle_queue_mapping(
                db, device, action, variables
            )

        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            ProvisioningLogs.update_status(
                db,
                log.id,
                ProvisioningLogStatus.failed,
                error=str(e),
                execution_time_ms=execution_time,
            )
            raise

        return ProvisioningLogs.get(db, log.id)

    @staticmethod
    def _handle_queue_mapping(
        db: Session,
        device: NasDevice,
        action: ProvisioningAction,
        variables: dict[str, Any],
    ) -> None:
        """
        Handle queue mapping creation/deactivation based on provisioning action.

        This integrates with the bandwidth monitoring system by maintaining
        the mapping between MikroTik queue names and subscriptions.
        """
        from app.services.queue_mapping import queue_mapping

        subscription_id = variables.get("subscription_id")
        if not subscription_id:
            return

        # Convert to UUID if string
        if isinstance(subscription_id, str):
            subscription_id = UUID(subscription_id)

        # Determine queue name from variables or generate from username
        queue_name = variables.get("queue_name")
        if not queue_name:
            username = variables.get("username")
            if username:
                queue_name = f"queue-{username}"
            else:
                queue_name = f"sub-{subscription_id}"

        if action == ProvisioningAction.create_user:
            # Create or update queue mapping for bandwidth monitoring
            queue_mapping.sync_from_provisioning(
                db,
                nas_device_id=device.id,
                queue_name=queue_name,
                subscription_id=subscription_id,
            )

        elif action in (ProvisioningAction.delete_user, ProvisioningAction.suspend_user):
            # Deactivate queue mappings when user is deleted or suspended
            queue_mapping.remove_subscription_mappings(db, subscription_id)

        elif action == ProvisioningAction.unsuspend_user:
            # Re-activate queue mapping when user is unsuspended
            queue_mapping.sync_from_provisioning(
                db,
                nas_device_id=device.id,
                queue_name=queue_name,
                subscription_id=subscription_id,
            )

    @staticmethod
    def _execute_ssh(device: NasDevice, command: str) -> str:
        """Execute command via SSH."""
        import paramiko

        if not device.management_ip and not device.ip_address:
            raise HTTPException(status_code=400, detail="Device has no management IP")

        if not device.ssh_username:
            raise HTTPException(status_code=400, detail="Device has no SSH credentials")

        host = device.management_ip or device.ip_address
        port = device.management_port or 22
        assert host is not None

        client = paramiko.SSHClient()
        if device.ssh_verify_host_key is False:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        try:
            if device.ssh_key:
                # Use SSH key authentication - decrypt key before use
                import io
                decrypted_key = decrypt_credential(device.ssh_key)
                key = paramiko.RSAKey.from_private_key(io.StringIO(decrypted_key))
                client.connect(
                    host, port=port, username=device.ssh_username, pkey=key, timeout=30
                )
            else:
                # Use password authentication - decrypt password before use
                decrypted_password = decrypt_credential(device.ssh_password)
                client.connect(
                    host,
                    port=port,
                    username=device.ssh_username,
                    password=decrypted_password,
                    timeout=30,
                )

            stdin, stdout, stderr = client.exec_command(command, timeout=60)
            output: str = stdout.read().decode()
            error: str = stderr.read().decode()

            if error and not output:
                raise Exception(f"SSH error: {error}")

            return output or error

        finally:
            client.close()

    @staticmethod
    def _execute_api(device: NasDevice, command: str, variables: dict) -> str:
        """Execute command via REST API."""
        import requests

        if not device.api_url:
            raise HTTPException(status_code=400, detail="Device has no API URL configured")

        # Build authentication - decrypt credentials before use
        auth = None
        headers = {}

        if device.api_token:
            decrypted_token = decrypt_credential(device.api_token)
            headers["Authorization"] = f"Bearer {decrypted_token}"
        elif device.api_username and device.api_password:
            decrypted_password = decrypt_credential(device.api_password)
            auth = (device.api_username, decrypted_password)

        # For MikroTik REST API, the command is the API path
        url = f"{device.api_url.rstrip('/')}/{command.lstrip('/')}"

        verify_tls = device.api_verify_tls if device.api_verify_tls is not None else False
        response = requests.post(
            url,
            json=variables,
            auth=auth,
            headers=headers,
            timeout=30,
            verify=verify_tls,
        )

        response.raise_for_status()
        return str(response.text)

    @staticmethod
    def backup_config(db: Session, nas_device_id: UUID, triggered_by: str = "system") -> NasConfigBackup:
        """
        Backup configuration from a NAS device.

        Args:
            db: Database session
            nas_device_id: Target NAS device ID
            triggered_by: Who triggered this backup

        Returns:
            NasConfigBackup with the configuration content
        """
        device = NasDevices.get(db, nas_device_id)

        # Determine backup method
        backup_method = device.backup_method or ConfigBackupMethod.ssh

        if backup_method == ConfigBackupMethod.ssh:
            config_content = DeviceProvisioner._backup_via_ssh(device)
        elif backup_method == ConfigBackupMethod.api:
            config_content = DeviceProvisioner._backup_via_api(device)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Backup method {backup_method.value} not implemented",
            )

        # Determine config format based on vendor
        config_format = "txt"
        if device.vendor == NasVendor.mikrotik:
            config_format = "rsc"

        # Create backup record
        backup = NasConfigBackups.create(
            db,
            NasConfigBackupCreate(
                nas_device_id=device.id,
                config_content=config_content,
                config_format=config_format,
                backup_method=backup_method,
                is_scheduled=False,
                is_manual=True,
            ),
        )

        return backup

    @staticmethod
    def _backup_via_ssh(device: NasDevice) -> str:
        """Backup configuration via SSH."""
        # Vendor-specific export commands
        if device.vendor == NasVendor.mikrotik:
            command = "/export"
        elif device.vendor == NasVendor.cisco:
            command = "show running-config"
        elif device.vendor == NasVendor.huawei:
            command = "display current-configuration"
        elif device.vendor == NasVendor.juniper:
            command = "show configuration"
        else:
            command = "show running-config"  # Generic fallback

        return DeviceProvisioner._execute_ssh(device, command)

    @staticmethod
    def _backup_via_api(device: NasDevice) -> str:
        """Backup configuration via REST API."""
        if device.vendor == NasVendor.mikrotik:
            # MikroTik REST API export endpoint
            return DeviceProvisioner._execute_api(device, "/rest/export", {})
        else:
            raise HTTPException(
                status_code=400,
                detail=f"API backup not implemented for vendor {device.vendor.value}",
            )


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

# Instantiate service singletons for easy import
nas_devices = NasDevices()
nas_config_backups = NasConfigBackups()
provisioning_templates = ProvisioningTemplates()
provisioning_logs = ProvisioningLogs()
radius_profiles = RadiusProfiles()
device_provisioner = DeviceProvisioner()
