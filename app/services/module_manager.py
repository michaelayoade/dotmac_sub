"""Feature module toggle management."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services.settings_cache import SettingsCache

logger = logging.getLogger(__name__)

_CACHE_DOMAIN = "modules"
_CACHE_KEY = "states"

MODULE_KEY_MAP: dict[str, str] = {
    "catalog": "module_catalog_enabled",
    "customer": "module_customer_enabled",
    "network": "module_network_enabled",
    "billing": "module_billing_enabled",
    "notifications": "module_notifications_enabled",
    "integrations": "module_integrations_enabled",
    "inventory": "module_inventory_enabled",
    "helpdesk": "module_helpdesk_enabled",
    "scheduling": "module_scheduling_enabled",
    "voice": "module_voice_enabled",
    "crm": "module_crm_enabled",
    "provisioning": "module_provisioning_enabled",
    "vpn": "module_vpn_enabled",
    "gis": "module_gis_enabled",
    "reports": "module_reports_enabled",
}

MODULE_FEATURE_MAP: dict[str, dict[str, str]] = {
    "billing": {
        "invoices": "module_billing_invoices_enabled",
        "payments": "module_billing_payments_enabled",
        "credit_notes": "module_billing_credit_notes_enabled",
        "payment_statements": "module_billing_payment_statements_enabled",
        "proforma_invoices": "module_billing_proforma_enabled",
        "voucher_cards": "module_billing_vouchers_enabled",
    },
    "catalog": {
        "internet_plans": "module_catalog_internet_plans_enabled",
        "fup_policies": "module_catalog_fup_enabled",
        "bundle_offers": "module_catalog_bundles_enabled",
        "one_time_charges": "module_catalog_onetime_enabled",
        "recurring_charges": "module_catalog_recurring_enabled",
    },
    "customer": {
        "additional_discounts": "module_customer_discounts_enabled",
        "voucher_management": "module_customer_vouchers_enabled",
        "services_view": "module_customer_services_enabled",
    },
    "network": {
        "network_sites": "module_network_sites_enabled",
        "cpe_management": "module_network_cpe_enabled",
        "tr069": "module_network_tr069_enabled",
        "router_management": "module_network_router_enabled",
        "ip_pools": "module_network_ip_pools_enabled",
        "hardware_inventory": "module_network_hardware_enabled",
    },
}

MODULE_LABELS: dict[str, str] = {
    "billing": "Billing",
    "catalog": "Catalog",
    "customer": "Customer",
    "network": "Network",
    "provisioning": "Provisioning",
    "vpn": "VPN",
    "gis": "GIS",
    "notifications": "Notifications",
    "reports": "Reports",
    "integrations": "Integrations",
    "inventory": "Inventory",
    "helpdesk": "Helpdesk",
    "scheduling": "Scheduling",
    "voice": "Voice/VoIP",
    "crm": "CRM",
}

FEATURE_LABELS: dict[str, str] = {
    "invoices": "Invoices",
    "payments": "Payments",
    "credit_notes": "Credit Notes",
    "payment_statements": "Payment Statements",
    "proforma_invoices": "Proforma Invoices",
    "voucher_cards": "Refill/Voucher Cards",
    "internet_plans": "Internet Plans",
    "fup_policies": "FUP Policies",
    "bundle_offers": "Bundle Offers",
    "one_time_charges": "One-Time Charges",
    "recurring_charges": "Recurring Charges",
    "additional_discounts": "Additional Discounts",
    "voucher_management": "Voucher Management",
    "services_view": "Customer Services View",
    "network_sites": "Network Sites",
    "cpe_management": "CPE Management",
    "tr069": "TR-069 (GenieACS)",
    "router_management": "Router Management",
    "ip_pools": "IPv4/IPv6 Network Pools",
    "hardware_inventory": "Hardware Inventory",
}

MODULE_ORDER = [
    "billing",
    "catalog",
    "customer",
    "network",
    "provisioning",
    "vpn",
    "gis",
    "notifications",
    "reports",
    "integrations",
    "inventory",
    "helpdesk",
    "scheduling",
    "voice",
    "crm",
]


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _resolve_module_flag(db: Session, setting_key: str, default: bool = True) -> bool:
    try:
        setting = domain_settings_service.modules_settings.get_by_key(db, setting_key)
    except HTTPException:
        return default
    if setting.value_json is not None:
        return _coerce_bool(setting.value_json, default=default)
    return _coerce_bool(setting.value_text, default=default)


def invalidate_module_cache() -> None:
    """Invalidate cached module state map."""
    SettingsCache.invalidate(_CACHE_DOMAIN, _CACHE_KEY)


def load_module_states(db: Session, *, force_refresh: bool = False) -> dict[str, bool]:
    """Load all module flags, using Redis cache for fast route checks."""
    if not force_refresh:
        cached = SettingsCache.get(_CACHE_DOMAIN, _CACHE_KEY)
        if isinstance(cached, dict):
            return {str(k): _coerce_bool(v) for k, v in cached.items()}

    states: dict[str, bool] = {}
    for module_name, setting_key in MODULE_KEY_MAP.items():
        states[module_name] = _resolve_module_flag(db, setting_key, default=True)

    SettingsCache.set(_CACHE_DOMAIN, _CACHE_KEY, states)
    return states


def is_module_enabled(db: Session, module_name: str) -> bool:
    """Return whether a module is enabled."""
    states = load_module_states(db)
    return states.get(module_name, True)


def load_feature_states(
    db: Session,
    *,
    force_refresh: bool = False,
) -> dict[str, bool]:
    cache_key = "feature_states"
    if not force_refresh:
        cached = SettingsCache.get(_CACHE_DOMAIN, cache_key)
        if isinstance(cached, dict):
            return {str(k): _coerce_bool(v) for k, v in cached.items()}

    states: dict[str, bool] = {}
    for features in MODULE_FEATURE_MAP.values():
        for feature_name, setting_key in features.items():
            states[feature_name] = _resolve_module_flag(db, setting_key, default=True)

    SettingsCache.set(_CACHE_DOMAIN, cache_key, states)
    return states


def list_payment_providers(db: Session) -> list[dict[str, Any]]:
    """Return payment-provider rows (id/name/provider_type/is_active).

    Backs the module-manager on/off toggles. Defensive: returns ``[]`` if the
    table can't be read (e.g. before the billing tables exist).
    """
    from app.models.billing import PaymentProvider

    try:
        rows = db.query(PaymentProvider).order_by(PaymentProvider.name).all()
    except Exception:
        logger.warning("Failed to load payment providers", exc_info=True)
        return []
    providers: list[dict[str, Any]] = []
    for row in rows:
        provider_type = getattr(row.provider_type, "value", str(row.provider_type))
        providers.append(
            {
                "id": str(row.id),
                "name": row.name,
                "provider_type": provider_type,
                "is_active": bool(row.is_active),
            }
        )
    return providers


def update_provider_flags(db: Session, *, payload: dict[str, bool]) -> None:
    """Set ``is_active`` per payment-provider id.

    Defensive: unknown or malformed ids are skipped; only commits if a row
    actually changed. Flipping a provider off here removes it from billing
    checkout.
    """
    if not payload:
        return
    from app.models.billing import PaymentProvider

    changed = False
    for provider_id, enabled in payload.items():
        try:
            provider = db.get(PaymentProvider, uuid.UUID(str(provider_id)))
        except (ValueError, TypeError):
            provider = None
        if provider is None:
            continue
        provider.is_active = bool(enabled)
        changed = True
    if changed:
        db.commit()


def module_manager_page_state(db: Session) -> dict[str, Any]:
    modules = load_module_states(db)
    features = load_feature_states(db)
    cards: list[dict[str, Any]] = []
    for module_name in MODULE_ORDER:
        feature_entries: list[dict[str, Any]] = []
        for feature_name in MODULE_FEATURE_MAP.get(module_name, {}):
            feature_entries.append(
                {
                    "name": feature_name,
                    "label": FEATURE_LABELS.get(
                        feature_name, feature_name.replace("_", " ").title()
                    ),
                    "enabled": bool(features.get(feature_name, True)),
                }
            )
        cards.append(
            {
                "name": module_name,
                "label": MODULE_LABELS.get(module_name, module_name.title()),
                "enabled": bool(modules.get(module_name, True)),
                "features": feature_entries,
            }
        )
    return {
        "module_cards": cards,
        "module_states": modules,
        "feature_states": features,
        "payment_providers": list_payment_providers(db),
    }


def _upsert_boolean_setting(db: Session, key: str, enabled: bool) -> None:
    domain_settings_service.modules_settings.upsert_by_key(
        db,
        key,
        DomainSettingUpdate(
            domain=SettingDomain.modules,
            value_type=SettingValueType.boolean,
            value_text="true" if enabled else "false",
            value_json=None,
            is_active=True,
        ),
    )


def update_module_flags(db: Session, *, payload: dict[str, bool]) -> None:
    for module_name, enabled in payload.items():
        setting_key = MODULE_KEY_MAP.get(module_name)
        if not setting_key:
            continue
        _upsert_boolean_setting(db, setting_key, bool(enabled))
    invalidate_module_cache()
    SettingsCache.invalidate(_CACHE_DOMAIN, "feature_states")


def update_feature_flags(db: Session, *, payload: dict[str, bool]) -> None:
    key_by_feature: dict[str, str] = {}
    for feature_map in MODULE_FEATURE_MAP.values():
        key_by_feature.update(feature_map)
    for feature_name, enabled in payload.items():
        setting_key = key_by_feature.get(feature_name)
        if not setting_key:
            continue
        _upsert_boolean_setting(db, setting_key, bool(enabled))
    SettingsCache.invalidate(_CACHE_DOMAIN, "feature_states")


def require_module_enabled(module_name: str) -> Callable[..., None]:
    """Dependency factory that returns 404 when a module is disabled."""

    def _dependency(db: Session = Depends(get_db)) -> None:
        if not is_module_enabled(db, module_name):
            raise HTTPException(status_code=404, detail="Not found")

    return _dependency
