"""View-context helpers for admin system settings page renders."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

ENFORCEMENT_DOMAIN = "enforcement"

# Domain groupings by business function
SETTINGS_DOMAIN_GROUPS = {
    "Enforcement": [ENFORCEMENT_DOMAIN],
    "Billing & Payments": ["billing", "collections", "usage"],
    "Notifications": ["notification", "comms"],
    "Services & Catalog": ["catalog", "subscriber", "provisioning", "lifecycle"],
    "Network": ["network", "network_monitoring", "radius", "bandwidth", "gis", "geocoding"],
    "Operations": ["workflow", "projects", "scheduler", "inventory"],
    "Security & System": ["auth", "audit", "imports"],
}


def settings_domains() -> list[dict]:
    domains = sorted(
        {spec.domain for spec in settings_spec.SETTINGS_SPECS},
        key=lambda domain: domain.value,
    )
    items = [
        {"value": domain.value, "label": domain.value.replace("_", " ").title()}
        for domain in domains
    ]
    items.insert(0, {"value": ENFORCEMENT_DOMAIN, "label": "Enforcement & FUP"})
    return items


def grouped_settings_domains() -> dict[str, list[dict]]:
    """Return settings domains grouped by business function."""
    all_domains = {d["value"]: d for d in settings_domains()}
    grouped = {}
    used = set()

    for group_name, domain_values in SETTINGS_DOMAIN_GROUPS.items():
        group_domains = []
        for dv in domain_values:
            if dv in all_domains:
                group_domains.append(all_domains[dv])
                used.add(dv)
        if group_domains:
            grouped[group_name] = group_domains

    other = [d for v, d in all_domains.items() if v not in used]
    if other:
        grouped["Other"] = sorted(other, key=lambda x: x["value"])

    return grouped


def resolve_settings_domain(value: str | None) -> SettingDomain:
    domains = settings_domains()
    default_value = domains[0]["value"] if domains else SettingDomain.auth.value
    raw = value or default_value
    if raw == ENFORCEMENT_DOMAIN:
        return SettingDomain.auth
    try:
        return SettingDomain(raw)
    except ValueError:
        return SettingDomain(default_value)


def enforcement_specs() -> list[settings_spec.SettingSpec]:
    ordered_keys = {
        SettingDomain.radius: [
            "coa_enabled",
            "coa_dictionary_path",
            "coa_timeout_sec",
            "coa_retries",
            "refresh_sessions_on_profile_change",
        ],
        SettingDomain.usage: [
            "usage_warning_enabled",
            "usage_warning_thresholds",
            "fup_action",
            "fup_throttle_radius_profile_id",
        ],
        SettingDomain.network: [
            "mikrotik_session_kill_enabled",
            "address_list_block_enabled",
            "default_mikrotik_address_list",
        ],
    }
    spec_map = {(spec.domain, spec.key): spec for spec in settings_spec.SETTINGS_SPECS}
    specs: list[settings_spec.SettingSpec] = []
    for domain, keys in ordered_keys.items():
        for key in keys:
            spec = spec_map.get((domain, key))
            if spec:
                specs.append(spec)
    return specs


def _resolve_raw_setting(db: Session, domain: SettingDomain, key: str) -> object | None:
    """Resolve the stored (pre-coercion/default) value for a setting key."""
    service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(domain)
    if not service:
        return None
    try:
        setting = service.get_by_key(db, key)
    except Exception:
        setting = None
    return settings_spec.extract_db_value(setting)


def build_settings_context(db: Session, domain_value: str | None) -> dict:
    if domain_value == ENFORCEMENT_DOMAIN:
        sections: list[dict] = []
        for domain, title in (
            (SettingDomain.radius, "RADIUS & Session Control"),
            (SettingDomain.usage, "Usage Policy"),
            (SettingDomain.network, "Network Enforcement"),
        ):
            specs = [spec for spec in enforcement_specs() if spec.domain == domain]
            rows = []
            for spec in specs:
                current = settings_spec.resolve_value(db, spec.domain, spec.key)
                raw = _resolve_raw_setting(db, spec.domain, spec.key)
                rows.append(
                    {
                        "spec": spec,
                        "value": current,
                        "raw": raw,
                        "description": spec.label,
                        "default": spec.default,
                    }
                )
            sections.append({"title": title, "rows": rows, "domain": domain.value})

        return {
            "domain": ENFORCEMENT_DOMAIN,
            "domains": settings_domains(),
            "grouped_domains": grouped_settings_domains(),
            "settings_rows": [],
            "sections": sections,
        }

    selected_domain = resolve_settings_domain(domain_value)
    specs = settings_spec.list_specs(selected_domain)
    rows = []
    for spec in specs:
        current = settings_spec.resolve_value(db, spec.domain, spec.key)
        raw = _resolve_raw_setting(db, spec.domain, spec.key)
        rows.append(
            {
                "spec": spec,
                "value": current,
                "raw": raw,
                "description": spec.label,
                "default": spec.default,
            }
        )
    return {
        "domain": selected_domain.value,
        "domains": settings_domains(),
        "grouped_domains": grouped_settings_domains(),
        "settings_rows": rows,
        "sections": [],
    }


def build_settings_page_context(
    request,
    db: Session,
    *,
    settings_context: dict,
    extra: dict | None = None,
) -> dict:
    """Compose common settings page context including CRM callback URLs."""
    from app.web.admin import get_current_user, get_sidebar_stats

    base_url = str(request.base_url).rstrip("/")
    context = {
        "request": request,
        **settings_context,
        "crm_meta_callback_url": base_url + "/webhooks/crm/meta",
        "crm_meta_oauth_redirect_url": base_url + "/admin/crm/meta/callback",
        "active_page": "settings",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }
    if extra:
        context.update(extra)
    return context
