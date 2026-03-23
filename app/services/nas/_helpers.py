"""
NAS helper functions for form options, tag manipulation, and device metadata.

Extracted from the monolithic nas.py service to improve maintainability.
"""

import ipaddress
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import (
    ConfigBackupMethod,
    ConnectionType,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
    ProvisioningAction,
)
from app.models.network_monitoring import PopSite
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _emit_nas_event(db: Session, event_name: str, payload: dict[str, Any]) -> None:
    """Emit a NAS event to the event system (non-blocking)."""
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        event_type = getattr(EventType, event_name, None)
        if event_type:
            emit_event(db, event_type, payload, actor="system")
    except Exception as e:
        logger.warning("Failed to emit NAS event %s: %s", event_name, e)


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


def list_pop_sites(
    db: Session, *, is_active: bool = True, limit: int = 500
) -> list[PopSite]:
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


def list_business_accounts(
    db: Session,
    *,
    ids: list[UUID] | None = None,
    limit: int = 500,
) -> list[Subscriber]:
    """Return business subscribers for NAS form and validation usage."""
    query = db.query(Subscriber).filter(
        Subscriber.metadata_["subscriber_category"].as_string()
        == SubscriberCategory.business.value
    )
    if ids:
        query = query.filter(Subscriber.id.in_(ids))
    return (
        query.order_by(Subscriber.company_name.asc(), Subscriber.display_name.asc())
        .limit(limit)
        .all()
    )


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
    business_accounts = list_business_accounts(db, limit=500)
    return {
        "pop_sites": pop_sites,
        "ip_pools": ip_pools,
        "business_accounts": business_accounts,
        "vendors": [{"value": v.value, "label": v.value.title()} for v in NasVendor],
        "statuses": [
            {"value": s.value, "label": s.value.title()} for s in NasDeviceStatus
        ],
        "connection_types": [
            {"value": ct.value, "label": ct.value.upper()} for ct in ConnectionType
        ],
        "backup_methods": [
            {"value": m.value, "label": m.value.upper()} for m in ConfigBackupMethod
        ],
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


def upsert_prefixed_tags(
    existing_tags: list[str] | None, prefix: str, values: list[str]
) -> list[str]:
    base = [tag for tag in (existing_tags or []) if not tag.startswith(prefix)]
    return base + [f"{prefix}{value}" for value in values if value]


def merge_single_tag(
    existing_tags: list[str] | None, prefix: str, value: str | None
) -> list[str] | None:
    merged = upsert_prefixed_tags(existing_tags, prefix, [value] if value else [])
    return merged or None


def merge_radius_pool_tags(
    existing_tags: list[str] | None, radius_pool_ids: list[str]
) -> list[str] | None:
    merged = upsert_prefixed_tags(existing_tags, "radius_pool:", radius_pool_ids)
    return merged or None


def merge_partner_org_tags(
    existing_tags: list[str] | None, partner_org_ids: list[str]
) -> list[str] | None:
    merged = upsert_prefixed_tags(existing_tags, "partner_org:", partner_org_ids)
    return merged or None


def extract_enhanced_fields(
    tags: list[str] | None,
) -> dict[str, str | list[str] | None]:
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
        "disabled_customers_address_list": prefixed_value_from_tags(
            tags, "disabled_customers_address_list:"
        ),
        "blocking_rules_enabled": prefixed_value_from_tags(
            tags, "blocking_rules_enabled:"
        ),
    }


def extract_mikrotik_status(tags: list[str] | None) -> dict[str, str | None]:
    return {
        "platform": prefixed_value_from_tags(tags, "mikrotik_status_platform:"),
        "board_name": prefixed_value_from_tags(tags, "mikrotik_status_board_name:"),
        "routeros_version": prefixed_value_from_tags(
            tags, "mikrotik_status_routeros_version:"
        ),
        "serial_number": prefixed_value_from_tags(
            tags, "mikrotik_status_serial_number:"
        ),
        "primary_mac": prefixed_value_from_tags(tags, "mikrotik_status_primary_mac:"),
        "architecture_name": prefixed_value_from_tags(
            tags, "mikrotik_status_architecture_name:"
        ),
        "cpu_model": prefixed_value_from_tags(tags, "mikrotik_status_cpu_model:"),
        "cpu_count": prefixed_value_from_tags(tags, "mikrotik_status_cpu_count:"),
        "cpu_frequency": prefixed_value_from_tags(
            tags, "mikrotik_status_cpu_frequency:"
        ),
        "total_hdd_space": prefixed_value_from_tags(
            tags, "mikrotik_status_total_hdd_space:"
        ),
        "free_hdd_space": prefixed_value_from_tags(
            tags, "mikrotik_status_free_hdd_space:"
        ),
        "cpu_usage": prefixed_value_from_tags(tags, "mikrotik_status_cpu_usage:"),
        "ipv6_status": prefixed_value_from_tags(tags, "mikrotik_status_ipv6_status:"),
        "last_status_check": prefixed_value_from_tags(
            tags, "mikrotik_status_last_check:"
        ),
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
    accounts = list_business_accounts(db, ids=valid_ids, limit=500)
    return [str(account.company_name or account.display_name or account.full_name) for account in accounts]


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
