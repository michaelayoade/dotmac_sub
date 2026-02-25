"""Admin web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.services import web_admin as web_admin_service
from app.web.admin.admin_hub import router as admin_hub_router
from app.web.admin.billing_accounts import router as billing_accounts_router
from app.web.admin.billing_arrangements import router as billing_arrangements_router
from app.web.admin.billing_channels import router as billing_channels_router
from app.web.admin.billing_collection_accounts import router as billing_collection_accounts_router
from app.web.admin.billing_credits import router as billing_credits_router
from app.web.admin.billing_dunning import router as billing_dunning_router
from app.web.admin.billing_invoice_actions import router as billing_invoice_actions_router
from app.web.admin.billing_invoice_batch import router as billing_invoice_batch_router
from app.web.admin.billing_invoice_bulk import router as billing_invoice_bulk_router
from app.web.admin.billing_invoices import router as billing_invoices_router
from app.web.admin.billing_payments import router as billing_payments_router
from app.web.admin.billing_providers import router as billing_providers_router
from app.web.admin.billing_reporting import router as billing_reporting_router
from app.web.admin.catalog import router as catalog_router
from app.web.admin.catalog_settings import router as catalog_settings_router
from app.web.admin.configuration import router as configuration_router
from app.web.admin.customers import contacts_router
from app.web.admin.customers import router as customers_router
from app.web.admin.dashboard import router as dashboard_router
from app.web.admin.gis import router as gis_router
from app.web.admin.integrations import router as integrations_router
from app.web.admin.legal import router as legal_router
from app.web.admin.nas import router as nas_router
from app.web.admin.network import router as network_router
from app.web.admin.network_core_devices import router as network_core_devices_router
from app.web.admin.network_fiber_plant import router as network_fiber_plant_router
from app.web.admin.network_fiber_splice import router as network_fiber_splice_router
from app.web.admin.network_cpes import router as network_cpes_router
from app.web.admin.network_monitoring import router as network_monitoring_router
from app.web.admin.network_olts_onts import router as network_olts_onts_router
from app.web.admin.network_ip_management import router as network_ip_management_router
from app.web.admin.network_dns_threats import router as network_dns_threats_router
from app.web.admin.network_speedtests import router as network_speedtests_router
from app.web.admin.network_weathermap import router as network_weathermap_router
from app.web.admin.network_tr069 import router as network_tr069_router
from app.web.admin.network_radius import router as network_radius_router
from app.web.admin.network_pop_sites import router as network_pop_sites_router
from app.web.admin.network_site_survey import router as network_site_survey_router
from app.web.admin.network_zones import router as network_zones_router
from app.web.admin.notifications import router as notifications_router
from app.web.admin.provisioning import router as provisioning_router
from app.web.admin.reports import router as reports_router
from app.web.admin.resellers import router as resellers_router
from app.web.admin.subscribers import router as subscribers_router
from app.web.admin.system import router as system_router
from app.web.admin.usage import router as usage_router
from app.web.admin.wireguard import router as wireguard_router
from app.web.auth.dependencies import require_web_auth

router = APIRouter(
    prefix="/admin",
    tags=["web-admin"],
    dependencies=[Depends(require_web_auth)],
)


def get_current_user(request: Request) -> dict:
    """Get current user from session/request."""
    return web_admin_service.get_current_user(request)


def get_sidebar_stats(db: Session) -> dict:
    """Get stats for sidebar badges."""
    return web_admin_service.get_sidebar_stats(db)


@router.get("")
def admin_root():
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/operations/service-orders")
def operations_service_orders_legacy():
    """Legacy route redirect for service orders list."""
    return RedirectResponse(url="/admin/provisioning/orders", status_code=307)


@router.get("/operations/service-orders/new")
def operations_service_orders_new_legacy(subscriber: str | None = None):
    """Legacy route redirect for service order create form."""
    url = "/admin/provisioning/orders/new"
    if subscriber:
        url = f"{url}?subscriber={subscriber}"
    return RedirectResponse(url=url, status_code=307)

# Include all admin sub-routers
router.include_router(dashboard_router)
router.include_router(subscribers_router)
router.include_router(customers_router)
router.include_router(contacts_router)
router.include_router(billing_invoices_router)
router.include_router(billing_accounts_router)
router.include_router(billing_arrangements_router)
router.include_router(billing_channels_router)
router.include_router(billing_collection_accounts_router)
router.include_router(billing_credits_router)
router.include_router(billing_dunning_router)
router.include_router(billing_invoice_actions_router)
router.include_router(billing_invoice_batch_router)
router.include_router(billing_invoice_bulk_router)
router.include_router(billing_payments_router)
router.include_router(billing_providers_router)
router.include_router(billing_reporting_router)
router.include_router(system_router)
router.include_router(network_router)
router.include_router(network_core_devices_router)
router.include_router(network_fiber_plant_router)
router.include_router(network_fiber_splice_router)
router.include_router(network_ip_management_router)
router.include_router(network_monitoring_router)
router.include_router(network_cpes_router)
router.include_router(network_olts_onts_router)
router.include_router(network_dns_threats_router)
router.include_router(network_speedtests_router)
router.include_router(network_weathermap_router)
router.include_router(network_tr069_router)
router.include_router(network_radius_router)
router.include_router(network_pop_sites_router)
router.include_router(network_site_survey_router)
router.include_router(network_zones_router)
router.include_router(catalog_router)
router.include_router(gis_router)
router.include_router(reports_router)
router.include_router(integrations_router)
router.include_router(resellers_router)
router.include_router(notifications_router)
router.include_router(wireguard_router, prefix="/network")
router.include_router(nas_router, prefix="/network")
router.include_router(legal_router, prefix="/system")
router.include_router(catalog_settings_router)
router.include_router(usage_router)
router.include_router(configuration_router, prefix="/system")
router.include_router(admin_hub_router, prefix="/system")
router.include_router(provisioning_router)

__all__ = ["router"]
