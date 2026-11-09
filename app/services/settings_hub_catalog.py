"""Presentation ownership for settings; never declares defaults or writes values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SettingsTab(str, Enum):
    SYSTEM = "system"
    SECURITY = "security"
    BILLING = "billing"
    NETWORK = "network"
    NOTIFICATIONS = "notifications"
    CATALOG = "catalog"
    OPERATIONS = "operations"
    INTEGRATIONS = "integrations"
    LOGS = "logs"


TAB_NAMES: dict[SettingsTab, str] = {
    SettingsTab.SYSTEM: "System",
    SettingsTab.SECURITY: "Security & Access",
    SettingsTab.BILLING: "Billing Setup",
    SettingsTab.NETWORK: "Network",
    SettingsTab.NOTIFICATIONS: "Notifications",
    SettingsTab.CATALOG: "Catalog & Subscribers",
    SettingsTab.OPERATIONS: "Operations",
    SettingsTab.INTEGRATIONS: "Integrations & AI",
    SettingsTab.LOGS: "Logs & Audit",
}


@dataclass(frozen=True, slots=True)
class SettingsSection:
    id: str
    name: str
    tab: SettingsTab
    domain: str

    @property
    def url(self) -> str:
        specialised = {
            "branding-values": "/admin/system/branding",
            "notification": "/admin/system/email",
            "company": "/admin/system/company-info",
        }
        return specialised.get(self.id, f"/admin/system/settings-hub/edit/{self.id}")


SECTIONS: tuple[SettingsSection, ...] = (
    SettingsSection("preferences", "General Preferences", SettingsTab.SYSTEM, "auth"),
    SettingsSection("portal", "Portal Domains", SettingsTab.SYSTEM, "auth"),
    SettingsSection("branding-values", "Branding", SettingsTab.SYSTEM, "comms"),
    SettingsSection("company", "Company Information", SettingsTab.SYSTEM, "billing"),
    SettingsSection("auth", "Authentication & Sessions", SettingsTab.SECURITY, "auth"),
    SettingsSection(
        "billing", "Billing Defaults & Policy", SettingsTab.BILLING, "billing"
    ),
    SettingsSection(
        "starter-accounting",
        "Starter Accounting & Governance",
        SettingsTab.BILLING,
        "billing",
    ),
    SettingsSection(
        "collections", "Collections & Dunning", SettingsTab.BILLING, "collections"
    ),
    SettingsSection(
        "catalog", "Catalog & Subscription Defaults", SettingsTab.CATALOG, "catalog"
    ),
    SettingsSection(
        "subscriber", "Subscriber Defaults", SettingsTab.CATALOG, "subscriber"
    ),
    SettingsSection(
        "referrals", "Referrals & Loyalty", SettingsTab.CATALOG, "subscriber"
    ),
    SettingsSection(
        "radius", "RADIUS & Session Control", SettingsTab.NETWORK, "radius"
    ),
    SettingsSection(
        "test-connection", "Test Connection", SettingsTab.NETWORK, "radius"
    ),
    SettingsSection(
        "network", "Network Defaults & Device Access", SettingsTab.NETWORK, "network"
    ),
    SettingsSection("vpn", "VPN & WireGuard", SettingsTab.NETWORK, "network"),
    SettingsSection(
        "router-health-policy", "Router Health Policy", SettingsTab.NETWORK, "network"
    ),
    SettingsSection(
        "router-health",
        "Router Health Thresholds",
        SettingsTab.NETWORK,
        "network_monitoring",
    ),
    SettingsSection(
        "network_monitoring",
        "Monitoring & Outages",
        SettingsTab.NETWORK,
        "network_monitoring",
    ),
    SettingsSection(
        "provisioning", "Provisioning", SettingsTab.NETWORK, "provisioning"
    ),
    SettingsSection("tr069", "TR-069 & ACS", SettingsTab.NETWORK, "tr069"),
    SettingsSection("snmp", "SNMP Discovery", SettingsTab.NETWORK, "snmp"),
    SettingsSection("bandwidth", "Bandwidth", SettingsTab.NETWORK, "bandwidth"),
    SettingsSection("usage", "Usage & Fair Usage Policy", SettingsTab.NETWORK, "usage"),
    SettingsSection("gis", "GIS & Coverage", SettingsTab.NETWORK, "gis"),
    SettingsSection("geocoding", "Geocoding", SettingsTab.NETWORK, "geocoding"),
    SettingsSection(
        "notification",
        "Notification Delivery Policy",
        SettingsTab.NOTIFICATIONS,
        "notification",
    ),
    SettingsSection(
        "comms", "Inbox & Communications", SettingsTab.INTEGRATIONS, "comms"
    ),
    SettingsSection(
        "integration", "Integrations & AI", SettingsTab.INTEGRATIONS, "integration"
    ),
    SettingsSection("workflow", "Workflow", SettingsTab.OPERATIONS, "workflow"),
    SettingsSection(
        "projects", "Projects & Quotes", SettingsTab.OPERATIONS, "projects"
    ),
    SettingsSection(
        "inventory", "Inventory Defaults", SettingsTab.OPERATIONS, "inventory"
    ),
    SettingsSection("lifecycle", "Lifecycle", SettingsTab.CATALOG, "lifecycle"),
    SettingsSection("imports", "Imports", SettingsTab.OPERATIONS, "imports"),
    SettingsSection("audit", "Audit Policy", SettingsTab.LOGS, "audit"),
    SettingsSection("scheduler", "Scheduler & Workers", SettingsTab.LOGS, "scheduler"),
)

_BY_ID = {section.id: section for section in SECTIONS}
_PREFERENCES = frozenset({"admin_mfa_required"})
_PORTAL = frozenset(
    {"selfcare_domain", "selfcare_redirect_root", "admin_domain", "reseller_domain"}
)


def section_by_id(section_id: str) -> SettingsSection:
    """Unknown sections fail closed rather than saving an unrelated domain."""
    return _BY_ID[section_id]


def setting_section(*, domain: str, key: str) -> SettingsSection:
    """Exactly one presentation destination for each registered identity."""
    section_id = domain
    if domain == "auth":
        if key in _PREFERENCES:
            section_id = "preferences"
        elif key in _PORTAL:
            section_id = "portal"
    elif domain == "comms" and key.startswith(
        ("brand_", "sidebar_logo", "favicon", "login_hero_")
    ):
        section_id = "branding-values"
    elif domain == "billing":
        if key.startswith(("company_", "partner_commission_")) or key == "billing_url":
            section_id = "company"
        elif key.startswith(("starter_", "sole_approver_")):
            section_id = "starter-accounting"
    elif domain == "subscriber" and key.startswith(("referral_", "loyalty_")):
        section_id = "referrals"
    elif domain == "radius" and key.startswith("test_connection_"):
        section_id = "test-connection"
    elif domain == "network":
        if key.startswith("wireguard_") or key.startswith("vpn_"):
            section_id = "vpn"
        elif key == "router_health_policy":
            section_id = "router-health-policy"
    elif domain == "network_monitoring" and key.startswith("router_health_"):
        section_id = "router-health"
    return section_by_id(section_id)
