"""Admin configuration hub web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import TaxRate
from app.models.catalog import (
    NasDevice,
    ProvisioningTemplate,
    RegionZone,
    PolicySet,
    UsageAllowance,
    SlaProfile,
    AddOn,
)
from app.models.connector import ConnectorConfig
from app.models.crm.team import CrmAgent
from app.models.projects import ProjectTemplate
from app.models.radius import RadiusServer
from app.models.webhook import WebhookEndpoint
from app.models.wireguard import WireGuardServer, WireGuardPeer
from app.models.network_monitoring import PopSite

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/configuration", tags=["web-admin-configuration"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str):
    from app.web.admin import get_sidebar_stats, get_current_user

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("", response_class=HTMLResponse)
def configuration_index(request: Request, db: Session = Depends(get_db)):
    """Configuration overview with cards linking to each section."""
    # Network configuration counts
    pop_sites_count = db.query(PopSite).count()
    vpn_servers_count = db.query(WireGuardServer).count()
    vpn_peers_count = db.query(WireGuardPeer).count()
    nas_devices_count = db.query(NasDevice).count()
    nas_templates_count = db.query(ProvisioningTemplate).count()
    radius_servers_count = db.query(RadiusServer).count()

    # Catalog configuration counts
    region_zones_count = db.query(RegionZone).count()
    policy_sets_count = db.query(PolicySet).count()
    usage_allowances_count = db.query(UsageAllowance).count()
    sla_profiles_count = db.query(SlaProfile).count()
    addons_count = db.query(AddOn).count()

    # Integrations configuration counts
    connectors_count = db.query(ConnectorConfig).count()
    webhooks_count = db.query(WebhookEndpoint).count()

    # Operations configuration counts
    project_templates_count = db.query(ProjectTemplate).count()

    # Business configuration counts
    tax_rates_count = db.query(TaxRate).count()
    crm_agents_count = db.query(CrmAgent).count()

    context = _base_context(request, db, active_page="configuration")
    context.update({
        # Network
        "pop_sites_count": pop_sites_count,
        "vpn_servers_count": vpn_servers_count,
        "vpn_peers_count": vpn_peers_count,
        "nas_devices_count": nas_devices_count,
        "nas_templates_count": nas_templates_count,
        "radius_servers_count": radius_servers_count,
        # Catalog
        "region_zones_count": region_zones_count,
        "policy_sets_count": policy_sets_count,
        "usage_allowances_count": usage_allowances_count,
        "sla_profiles_count": sla_profiles_count,
        "addons_count": addons_count,
        # Integrations
        "connectors_count": connectors_count,
        "webhooks_count": webhooks_count,
        # Operations
        "project_templates_count": project_templates_count,
        # Business
        "tax_rates_count": tax_rates_count,
        "crm_agents_count": crm_agents_count,
    })
    return templates.TemplateResponse("admin/system/configuration/index.html", context)
