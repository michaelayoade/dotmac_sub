from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    audit as audit_service,
    analytics as analytics_service,
    bandwidth as bandwidth_service,
    billing as billing_service,
    catalog as catalog_service,
    collections as collections_service,
    connector as connector_service,
    external as external_service,
    gis as gis_service,
    integration as integration_service,
    lifecycle as lifecycle_service,
    network as network_service,
    network_monitoring as monitoring_service,
    notification as notification_service,
    provisioning as provisioning_service,
    qualification as qualification_service,
    radius as radius_service,
    rbac as rbac_service,
    scheduler as scheduler_service,
    settings_api as settings_api_service,
    snmp as snmp_service,
    subscription_engine as subscription_engine_service,
    tr069 as tr069_service,
    usage as usage_service,
    webhook as webhook_service,
    subscriber as subscriber_service,
)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/web", tags=["web"])


def _render(request: Request, title: str, items):
    return templates.TemplateResponse(
        "domain.html",
        {"request": request, "title": title, "items": items},
    )


@router.get("/network", response_class=HTMLResponse)
def network_home(request: Request, db: Session = Depends(get_db)):
    items = network_service.cpe_devices.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Network Inventory", items)


@router.get("/network-monitoring", response_class=HTMLResponse)
def network_monitoring_home(request: Request, db: Session = Depends(get_db)):
    items = monitoring_service.network_devices.list(
        db=db,
        pop_site_id=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=25,
        offset=0,
    )
    return _render(request, "Network Monitoring", items)


@router.get("/provisioning", response_class=HTMLResponse)
def provisioning_home(request: Request, db: Session = Depends(get_db)):
    items = provisioning_service.service_orders.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Provisioning", items)


@router.get("/usage", response_class=HTMLResponse)
def usage_home(request: Request, db: Session = Depends(get_db)):
    items = usage_service.usage_records.list(
        db=db,
        subscription_id=None,
        quota_bucket_id=None,
        order_by="recorded_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Usage Records", items)


@router.get("/radius", response_class=HTMLResponse)
def radius_home(request: Request, db: Session = Depends(get_db)):
    items = radius_service.radius_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=25,
        offset=0,
    )
    return _render(request, "RADIUS", items)


@router.get("/collections", response_class=HTMLResponse)
def collections_home(request: Request, db: Session = Depends(get_db)):
    items = collections_service.dunning_cases.list(
        db=db,
        account_id=None,
        status=None,
        order_by="started_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Collections", items)


@router.get("/lifecycle", response_class=HTMLResponse)
def lifecycle_home(request: Request, db: Session = Depends(get_db)):
    items = lifecycle_service.subscription_lifecycle_events.list(
        db=db,
        subscription_id=None,
        event_type=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Lifecycle Events", items)


@router.get("/tr069", response_class=HTMLResponse)
def tr069_home(request: Request, db: Session = Depends(get_db)):
    items = tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=25,
        offset=0,
    )
    return _render(request, "TR-069", items)


@router.get("/snmp", response_class=HTMLResponse)
def snmp_home(request: Request, db: Session = Depends(get_db)):
    items = snmp_service.snmp_targets.list(
        db=db,
        device_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "SNMP", items)


@router.get("/bandwidth", response_class=HTMLResponse)
def bandwidth_home(request: Request, db: Session = Depends(get_db)):
    items = bandwidth_service.bandwidth_samples.list(
        db=db,
        subscription_id=None,
        device_id=None,
        interface_id=None,
        order_by="sample_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Bandwidth Samples", items)


@router.get("/subscription-engines", response_class=HTMLResponse)
def subscription_engines_home(request: Request, db: Session = Depends(get_db)):
    items = subscription_engine_service.subscription_engines.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=25,
        offset=0,
    )
    return _render(request, "Subscription Engines", items)


@router.get("/subscribers", response_class=HTMLResponse)
def subscribers_home(request: Request, db: Session = Depends(get_db)):
    items = subscriber_service.subscribers.list(
        db=db,
        organization_id=None,
        subscriber_type=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Subscribers", items)


@router.get("/rbac", response_class=HTMLResponse)
def rbac_home(request: Request, db: Session = Depends(get_db)):
    items = rbac_service.roles.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Roles", items)


@router.get("/catalog", response_class=HTMLResponse)
def catalog_home(request: Request, db: Session = Depends(get_db)):
    items = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Service Plans", items)


@router.get("/billing", response_class=HTMLResponse)
def billing_home(request: Request, db: Session = Depends(get_db)):
    items = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="issued_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Billing Invoices", items)


@router.get("/notifications", response_class=HTMLResponse)
def notifications_home(request: Request, db: Session = Depends(get_db)):
    items = notification_service.notifications.list(
        db=db,
        channel=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Notifications", items)


@router.get("/integrations", response_class=HTMLResponse)
def integrations_home(request: Request, db: Session = Depends(get_db)):
    items = integration_service.integration_targets.list(
        db=db,
        target_type=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Integration Targets", items)


@router.get("/connectors", response_class=HTMLResponse)
def connectors_home(request: Request, db: Session = Depends(get_db)):
    items = connector_service.connector_configs.list(
        db=db,
        connector_type=None,
        auth_type=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Connector Configs", items)


@router.get("/webhooks", response_class=HTMLResponse)
def webhooks_home(request: Request, db: Session = Depends(get_db)):
    items = webhook_service.webhook_endpoints.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Webhook Endpoints", items)


@router.get("/gis", response_class=HTMLResponse)
def gis_home(request: Request, db: Session = Depends(get_db)):
    items = gis_service.geo_locations.list(
        db=db,
        location_type=None,
        address_id=None,
        pop_site_id=None,
        is_active=None,
        min_latitude=None,
        min_longitude=None,
        max_latitude=None,
        max_longitude=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "GIS Locations", items)


@router.get("/geocoding", response_class=HTMLResponse)
def geocoding_home(request: Request, db: Session = Depends(get_db)):
    items = settings_api_service.list_geocoding_settings(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Geocoding Settings", items)


@router.get("/qualification", response_class=HTMLResponse)
def qualification_home(request: Request, db: Session = Depends(get_db)):
    items = qualification_service.coverage_areas.list(
        db=db,
        zone_key=None,
        buildout_status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Coverage Areas", items)


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler_home(request: Request, db: Session = Depends(get_db)):
    items = scheduler_service.scheduled_tasks.list(
        db=db,
        enabled=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Scheduled Tasks", items)


@router.get("/audit", response_class=HTMLResponse)
def audit_home(request: Request, db: Session = Depends(get_db)):
    items = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "Audit Events", items)


@router.get("/external-references", response_class=HTMLResponse)
def external_references_home(request: Request, db: Session = Depends(get_db)):
    items = external_service.external_references.list(
        db=db,
        connector_config_id=None,
        entity_type=None,
        entity_id=None,
        external_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "External References", items)


@router.get("/workflow", response_class=HTMLResponse)
def workflow_home(request: Request, db: Session = Depends(get_db)):
    """Workflow page - SLA management removed, returns placeholder."""
    return _render(request, "Workflow", [])


@router.get("/analytics", response_class=HTMLResponse)
def analytics_home(request: Request, db: Session = Depends(get_db)):
    items = analytics_service.kpi_configs.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return _render(request, "KPI Configs", items)
