"""Admin network zones web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import network as network_service
from app.services import web_network_zones as web_network_zones_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(
    request: Request,
    db: Session,
    active_page: str,
    active_menu: str = "network",
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/zones", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def zones_list(
    request: Request, status: str | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    """List all network zones."""
    page_data = web_network_zones_service.list_page_data(db, status)
    context = _base_context(request, db, active_page="zones")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/zones/index.html", context)


@router.get("/zones/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def zone_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Show new zone form."""
    form_context = web_network_zones_service.build_form_context(
        db,
        zone=None,
        action_url="/admin/network/zones",
    )
    context = _base_context(request, db, active_page="zones")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/zones/form.html", context)


@router.post("/zones", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def zone_create(request: Request, db: Session = Depends(get_db)) -> Response:
    """Create a new zone."""
    form = parse_form_data_sync(request)
    values = web_network_zones_service.parse_form_values(form)
    error = web_network_zones_service.validate_form(values)
    if error:
        form_context = web_network_zones_service.build_form_context(
            db,
            zone=None,
            action_url="/admin/network/zones",
            error=error,
        )
        context = _base_context(request, db, active_page="zones")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/zones/form.html", context)

    zone = web_network_zones_service.create_zone(db, values)
    return RedirectResponse(f"/admin/network/zones/{zone.id}", status_code=303)


@router.get("/zones/{zone_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def zone_detail(
    request: Request,
    zone_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show zone detail page."""
    page_data = web_network_zones_service.detail_page_data(db, zone_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Network zone not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="zones")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/zones/detail.html", context)


@router.get("/zones/{zone_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def zone_edit(
    request: Request,
    zone_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show zone edit form."""
    zone = network_service.network_zones.get_or_none(db, zone_id)
    if not zone:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Network zone not found"},
            status_code=404,
        )
    form_context = web_network_zones_service.build_form_context(
        db,
        zone=zone,
        action_url=f"/admin/network/zones/{zone.id}",
    )
    context = _base_context(request, db, active_page="zones")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/zones/form.html", context)


@router.post("/zones/{zone_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def zone_update(
    request: Request,
    zone_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Update an existing zone."""
    zone = network_service.network_zones.get_or_none(db, zone_id)
    if not zone:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Network zone not found"},
            status_code=404,
        )
    form = parse_form_data_sync(request)
    values = web_network_zones_service.parse_form_values(form)
    error = web_network_zones_service.validate_form(values)
    if error:
        form_context = web_network_zones_service.build_form_context(
            db,
            zone=zone,
            action_url=f"/admin/network/zones/{zone.id}",
            error=error,
        )
        context = _base_context(request, db, active_page="zones")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/zones/form.html", context)

    web_network_zones_service.update_zone(db, zone_id, values)
    return RedirectResponse(f"/admin/network/zones/{zone_id}", status_code=303)
