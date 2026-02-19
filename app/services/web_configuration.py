"""Service helpers for admin configuration pages."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import TaxRate
from app.models.catalog import (
    AddOn,
    NasDevice,
    PolicySet,
    ProvisioningTemplate,
    RegionZone,
    SlaProfile,
    UsageAllowance,
)
from app.models.connector import ConnectorConfig
from app.models.network_monitoring import PopSite
from app.models.radius import RadiusServer
from app.models.webhook import WebhookEndpoint
from app.models.wireguard import WireGuardPeer, WireGuardServer


def get_configuration_counts(db: Session) -> dict[str, int]:
    """Return section counts for the admin configuration overview page."""
    def _count(model) -> int:
        return db.scalar(select(func.count()).select_from(model)) or 0

    return {
        "pop_sites_count": _count(PopSite),
        "vpn_servers_count": _count(WireGuardServer),
        "vpn_peers_count": _count(WireGuardPeer),
        "nas_devices_count": _count(NasDevice),
        "nas_templates_count": _count(ProvisioningTemplate),
        "radius_servers_count": _count(RadiusServer),
        "region_zones_count": _count(RegionZone),
        "policy_sets_count": _count(PolicySet),
        "usage_allowances_count": _count(UsageAllowance),
        "sla_profiles_count": _count(SlaProfile),
        "addons_count": _count(AddOn),
        "connectors_count": _count(ConnectorConfig),
        "webhooks_count": _count(WebhookEndpoint),
        "tax_rates_count": _count(TaxRate),
    }
