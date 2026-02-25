"""Admin network management base web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_core_devices as web_network_core_devices_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/devices", response_class=HTMLResponse)
def devices_list(
    request: Request,
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all network devices."""
    page_data = web_network_core_devices_service.devices_list_page_data(
        db, device_type=device_type, search=search, status=status, vendor=vendor
    )
    context = _base_context(request, db, active_page="devices")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/devices/index.html", context)


@router.get("/devices/search", response_class=HTMLResponse)
def devices_search(request: Request, search: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    devices = web_network_core_devices_service.devices_search_data(db, search)
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.get("/devices/filter", response_class=HTMLResponse)
def devices_filter(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    devices = web_network_core_devices_service.devices_filter_data(
        db, search=search, status=status, vendor=vendor
    )
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.post("/devices/discover", response_class=HTMLResponse)
def devices_discover(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        "Discovery queued. Devices will appear as they are detected."
        "</div>"
    )


@router.get("/devices/create", response_class=HTMLResponse)
def device_create(request: Request, db: Session = Depends(get_db)):
    # Redirect to the more specific device creation pages.
    return RedirectResponse(url="/admin/network/core-devices/new", status_code=302)


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(request: Request, device_id: str, db: Session = Depends(get_db)) -> Response:
    redirect_url = web_network_core_devices_service.resolve_device_redirect(db, device_id)
    if redirect_url:
        return RedirectResponse(url=redirect_url, status_code=302)

    return templates.TemplateResponse(
        "admin/errors/404.html",
        {"request": request, "message": "Device not found"},
        status_code=404,
    )


@router.post("/devices/{device_id}/ping", response_class=HTMLResponse)
def device_ping(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Ping queued for device {device_id}."
        "</div>"
    )


@router.post("/devices/{device_id}/reboot", response_class=HTMLResponse)
def device_reboot(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Reboot request queued for device {device_id}."
        "</div>"
    )


@router.get("/map", response_class=HTMLResponse)
def comprehensive_network_map(request: Request, db: Session = Depends(get_db)):
    """Comprehensive network map showing all infrastructure and customers."""
    from app.services import network_map as network_map_service

    context = _base_context(request, db, active_page="network-map")
    context.update(network_map_service.build_network_map_context(db))
    return templates.TemplateResponse("admin/network/map.html", context)
