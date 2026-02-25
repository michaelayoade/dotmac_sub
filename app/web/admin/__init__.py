"""Admin web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.services import web_admin as web_admin_service
from app.web.admin.admin_hub import router as admin_hub_router
from app.web.admin.billing import router as billing_router
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
router.include_router(billing_router)
router.include_router(system_router)
router.include_router(network_router)
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
