"""Service helpers for the centralized settings hub page."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import settings_spec
from app.services.settings_hub_catalog import (
    SECTIONS,
    TAB_NAMES,
    SettingsSection,
    SettingsTab,
    section_by_id,
    setting_section,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings Hub category definitions
# ---------------------------------------------------------------------------

SETTINGS_CATEGORIES: list[dict] = [
    {
        "id": "system",
        "name": "System",
        "icon": "M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z",
        "color": "slate",
        "description": "Core system configuration and security",
        "links": [
            {
                "name": "Control Plane",
                "url": "/admin/system/control-plane",
                "description": "Effective state, provenance, health, scope, and audit",
            },
            {
                "name": "Custom Fields",
                "url": "/admin/custom-fields",
                "description": "Define installation-specific fields for registered records",
                "permission": "custom_fields:hub:read",
            },
            {
                "name": "Setup Customer Connection Types",
                "url": "/admin/system/customer-connection-types",
                "description": "Manage customer last-mile classifications",
                "permission": "system:settings:read",
            },
            {
                "name": "Preferences & Security",
                "url": "/admin/system/config/preferences",
                "description": "Landing page, 2FA, portal title",
            },
            {
                "name": "Branding & Assets",
                "url": "/admin/system/branding",
                "description": "Sidebar logos, dark logo, favicon",
            },
            {
                "name": "Company Information",
                "url": "/admin/system/company-info",
                "description": "Company name, address, bank details",
            },
            {
                "name": "Email / SMTP",
                "url": "/admin/system/email",
                "description": "Outbound email transport settings",
            },
            {
                "name": "Users & Roles",
                "url": "/admin/system/users",
                "description": "Admin accounts, roles, permissions",
            },
            {
                "name": "API Keys",
                "url": "/admin/system/api-keys",
                "description": "Manage API access tokens",
            },
            {
                "name": "Resellers",
                "url": "/admin/resellers",
                "description": "Manage reseller and partner accounts",
            },
            {
                "name": "Secrets (OpenBao)",
                "url": "/admin/system/secrets",
                "description": "Encrypted secrets vault — API keys, passwords, tokens",
            },
            {
                "name": "Webhooks",
                "url": "/admin/integrations/webhooks",
                "description": "Capability-bound event delivery endpoints",
            },
            {
                "name": "ERP Material Catalogue",
                "url": "/admin/integrations/erp-material-catalog",
                "description": "Review ERP items, warehouses, eligibility, and synchronization",
                "permission": "system:settings:read",
            },
            {
                "name": "System Information",
                "url": "/admin/system/about",
                "description": "Version, environment, diagnostics",
            },
            {
                "name": "What's New",
                "url": "/admin/system/whats-new",
                "description": "Manage admin dashboard feature slides",
            },
            {
                "name": "Ticket Settings",
                "url": "/admin/system/ticket-settings",
                "description": "Support ticket statuses, priorities, and types",
            },
            {
                "name": "Service Teams",
                "url": "/admin/system/service-teams",
                "description": "Operational team identity, lifecycle, and membership",
                "permission": "operations:service_team:read",
            },
        ],
    },
    {
        "id": "billing",
        "name": "Billing Setup",
        "icon": "M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z",
        "color": "emerald",
        "description": "Tenant-wide billing configuration, payment rails, and finance setup",
        "links": [
            {
                "name": "Billing Settings",
                "url": "/admin/system/config/billing",
                "description": "Billing day, periods, invoice numbering",
            },
            {
                "name": "Direct Bank Transfer",
                "url": "/admin/system/config/direct-bank-transfer",
                "description": "Customer transfer account details and instructions",
            },
            {
                "name": "Payment Methods",
                "url": "/admin/system/config/payment-methods",
                "description": "Available customer payment methods and entry points",
            },
            {
                "name": "Online Payment Gateways",
                "url": "/admin/integrations/marketplace",
                "description": "Install, validate, and order gateway connectors",
            },
            {
                "name": "Payment Channels",
                "url": "/admin/settings/billing/payment-channels",
                "description": "Customer-facing payment options and defaults",
            },
            {
                "name": "Collection Accounts",
                "url": "/admin/settings/billing/collection-accounts",
                "description": "Bank and cash accounts where funds settle",
            },
            {
                "name": "Channel Mappings",
                "url": "/admin/settings/billing/payment-channel-accounts",
                "description": "Map channels to settlement accounts",
            },
            {
                "name": "Tax Rates",
                "url": "/admin/billing/tax-rates",
                "description": "Operational tax rate records used by billing",
            },
            {
                "name": "Tax Accounting",
                "url": "/admin/billing/tax-accounting",
                "description": "Tax source register and WHT evidence lifecycle",
            },
            {
                "name": "Tax Configuration",
                "url": "/admin/system/config/tax",
                "description": "Tax configuration overview and policy",
            },
            {
                "name": "Billing Reminders",
                "url": "/admin/system/config/reminders",
                "description": "Multi-wave reminder schedule",
            },
            {
                "name": "Billing Notifications",
                "url": "/admin/system/config/billing-notifications",
                "description": "Notification copy and recurring/prepaid billing events",
            },
            {
                "name": "Plan Change Rules",
                "url": "/admin/system/config/plan-change",
                "description": "Refund policy, upgrade/downgrade fees",
            },
            {
                "name": "Ledger",
                "url": "/admin/billing/ledger",
                "description": "Finance ledger entries and exports",
            },
        ],
    },
    {
        "id": "network",
        "name": "Network",
        "icon": "M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01",
        "color": "blue",
        "description": "RADIUS, CPE, monitoring, and IP management",
        "links": [
            {
                "name": "RADIUS Configuration",
                "url": "/admin/system/config/radius",
                "description": "Reject IPs, MAC binding, debug",
            },
            {
                "name": "Walled Garden",
                "url": "/admin/system/config/walled-garden",
                "description": "Resources reachable while captive; router readiness",
                "permission": "system:settings:read",
            },
            {
                "name": "Monitoring",
                "url": "/admin/system/config/monitoring",
                "description": "Vendors, device types, alert groups",
            },
            {
                "name": "Customer Regions",
                "url": "/admin/customer-regions",
                "description": "Radius, center coordinates, colors, and overlap rules",
                "permission": "gis:map:view",
            },
            {
                "name": "NAS Types",
                "url": "/admin/system/config/nas-types",
                "description": "Supported NAS vendor types",
            },
        ],
    },
    {
        "id": "notifications",
        "name": "Notifications",
        "icon": "M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9",
        "color": "rose",
        "description": "Email, SMS, and notification templates",
        "links": [
            {
                "name": "Notification Templates",
                "url": "/admin/system/config/templates",
                "description": "Email and SMS templates with variables",
            },
            {
                "name": "Bulk Notification Setup",
                "url": "/admin/notifications/setup",
                "description": "Channel readiness and bulk notification setup",
            },
            {
                "name": "Billing Notifications",
                "url": "/admin/system/config/billing-notifications",
                "description": "Prepaid and recurring notification waves",
            },
            {
                "name": "Email / SMTP",
                "url": "/admin/system/email",
                "description": "SMTP transport and rate limits",
            },
        ],
    },
    {
        "id": "catalog",
        "name": "Catalog & Subscribers",
        "icon": "M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10",
        "color": "violet",
        "description": "Subscriber defaults, portal, catalog options",
        "links": [
            {
                "name": "Customer Portal",
                "url": "/admin/system/config/portal",
                "description": "Portal branding, menu, field permissions",
            },
            {
                "name": "Catalog Settings",
                "url": "/admin/catalog/settings",
                "description": "Region zones, SLA profiles, usage",
            },
        ],
    },
    {
        "id": "logs",
        "name": "Logs & Audit",
        "icon": "M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z",
        "color": "amber",
        "description": "Audit trails, delivery logs, and system events",
        "links": [
            {
                "name": "Audit Log",
                "url": "/admin/system/audit",
                "description": "Admin operations audit trail",
            },
            {
                "name": "Scheduler",
                "url": "/admin/system/scheduler",
                "description": "Background task logs and schedule",
            },
        ],
    },
]


def build_settings_hub_context(db: Session) -> dict:
    """Project all registered settings and existing management destinations."""
    categories = {
        category["id"]: deepcopy(category) for category in SETTINGS_CATEGORIES
    }
    for tab in SettingsTab:
        if tab.value not in categories:
            categories[tab.value] = {
                "id": tab.value,
                "name": TAB_NAMES[tab],
                "icon": "",
                "color": "slate",
                "description": "Configuration and management",
                "links": [],
            }
    moved = {
        "Users & Roles": "security",
        "API Keys": "security",
        "Secrets (OpenBao)": "security",
        "Webhooks": "integrations",
        "ERP Material Catalogue": "integrations",
        "Ticket Settings": "operations",
        "Service Teams": "operations",
        "Email / SMTP": "notifications",
        "Control Plane": "logs",
        "Resellers": "catalog",
    }
    for category in list(categories.values()):
        retained = []
        for link in category["links"]:
            destination = moved.get(link["name"], category["id"])
            if destination != category["id"]:
                if not any(
                    item["url"] == link["url"]
                    for item in categories[destination]["links"]
                ):
                    categories[destination]["links"].append(link)
            else:
                retained.append(link)
        category["links"] = retained
    present = {
        setting_section(domain=str(spec.domain), key=spec.key).id
        for spec in settings_spec.SETTINGS_SPECS
    }
    for section in SECTIONS:
        # Branding and notification values already have complete specialised editors.
        if section.id not in present or section.id in {
            "branding-values",
            "notification",
            "company",
        }:
            continue
        categories[section.tab.value]["links"].append(
            {
                "name": section.name,
                "url": section.url,
                "description": "Registered settings, defaults, and operational policy",
                "permission": "system:settings:read",
            }
        )
    categories["system"]["links"].append(
        {
            "name": "Optional Modules & Features",
            "url": "/admin/system/modules",
            "description": "Canonical optional-capability and safety controls",
            "permission": "system:settings:read",
        }
    )
    categories["billing"]["links"].append(
        {
            "name": "Customer Bank Accounts",
            "url": "/admin/system/settings-hub/bank-accounts",
            "description": "Customer bank payment accounts and provider tokens",
            "permission": "system:settings:read",
        }
    )
    categories["operations"]["links"].append(
        {
            "name": "Recovery & Retention",
            "url": "/admin/system/tools/restore",
            "description": "Recovery tools and restore retention policy",
            "permission": "system:settings:read",
        }
    )
    categories["operations"]["links"].append(
        {
            "name": "Geocoding Jobs",
            "url": "/admin/system/tools/geocode",
            "description": "Batch address geocoding and job history",
            "permission": "system:settings:read",
        }
    )
    search_terms: dict[str, list[str]] = {}
    for spec in settings_spec.SETTINGS_SPECS:
        section = setting_section(domain=str(spec.domain), key=spec.key)
        search_terms.setdefault(section.url, []).extend((spec.key, spec.label or ""))
    for category in categories.values():
        for link in category["links"]:
            link["search_terms"] = " ".join(search_terms.get(link["url"], ()))
    return {"categories": [categories[tab.value] for tab in SettingsTab]}


@dataclass(frozen=True, slots=True)
class HubSectionQuery:
    section_id: str


@dataclass(frozen=True, slots=True)
class HubSettingRow:
    spec: settings_spec.SettingSpec
    value: object
    configured: bool
    source: str


@dataclass(frozen=True, slots=True)
class HubSectionView:
    section: SettingsSection
    rows: tuple[HubSettingRow, ...]


@dataclass(frozen=True, slots=True)
class HubBankAccountsQuery:
    account_id: UUID | None = None
    offset: int = 0
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class HubBankAccountRow:
    id: UUID
    account_id: UUID
    bank_name: str
    account_type: str
    account_last4: str
    routing_last4: str
    is_default: bool
    is_active: bool


def read_bank_accounts(
    db: Session,
    *,
    query: HubBankAccountsQuery,
) -> tuple[HubBankAccountRow, ...]:
    """Project account metadata; provider tokens never enter the page context."""
    from app.services import billing

    accounts = billing.bank_accounts.list(
        db,
        account_id=str(query.account_id) if query.account_id else None,
        is_active=query.is_active,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=query.offset,
    )
    return tuple(
        HubBankAccountRow(
            id=account.id,
            account_id=account.account_id,
            bank_name=account.bank_name or "",
            account_type=account.account_type.value,
            account_last4=account.account_last4 or "",
            routing_last4=account.routing_last4 or "",
            is_default=account.is_default,
            is_active=account.is_active,
        )
        for account in accounts
    )


def section_specs(*, query: HubSectionQuery) -> tuple[settings_spec.SettingSpec, ...]:
    section = section_by_id(query.section_id)
    return tuple(
        spec
        for spec in settings_spec.SETTINGS_SPECS
        if setting_section(domain=str(spec.domain), key=spec.key) == section
    )


def read_section(db: Session, *, query: HubSectionQuery) -> HubSectionView:
    rows = []
    for spec in section_specs(query=query):
        value, source = settings_spec.resolve_with_source(db, spec.domain, spec.key)
        rows.append(
            HubSettingRow(
                spec=spec,
                value=None if spec.is_secret else value,
                configured=value is not None and value != "",
                source=source,
            )
        )
    return HubSectionView(section=section_by_id(query.section_id), rows=tuple(rows))
