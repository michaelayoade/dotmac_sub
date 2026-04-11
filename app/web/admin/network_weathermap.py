"""Admin network topology routes (replaces legacy weathermap)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_topology as web_topology_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/topology",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def network_topology(
    request: Request,
    group: str | None = None,
    site: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Network topology visualization page."""
    context = _base_context(request, db, active_page="topology")
    context.update(
        web_topology_service.topology_page_context(db, group=group, site=site)
    )
    return templates.TemplateResponse("admin/network/topology/index.html", context)


# Legacy weathermap URL redirect
@router.get("/weathermap", response_class=HTMLResponse)
def network_weathermap_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/network/topology", status_code=301)


# ── Link CRUD ────────────────────────────────────────────────────────


@router.get(
    "/topology/links/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def topology_link_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="topology")
    context.update(
        web_topology_service.link_form_context(
            db,
            link=None,
            action_url="/admin/network/topology/links/new",
        )
    )
    return templates.TemplateResponse("admin/network/topology/link_form.html", context)


@router.post(
    "/topology/links/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def topology_link_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    data = web_topology_service.parse_link_form(form)
    try:
        web_topology_service.create_link(db, data=data)
        return RedirectResponse(url="/admin/network/topology", status_code=303)
    except (ValueError, Exception) as exc:
        context = _base_context(request, db, active_page="topology")
        context.update(
            web_topology_service.link_form_context(
                db,
                link=data,
                action_url="/admin/network/topology/links/new",
                error=str(exc),
            )
        )
        return templates.TemplateResponse(
            "admin/network/topology/link_form.html", context
        )


@router.get(
    "/topology/links/{link_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def topology_link_edit(request: Request, link_id: str, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="topology")
    context.update(web_topology_service.link_edit_context(db, link_id))
    return templates.TemplateResponse("admin/network/topology/link_form.html", context)


@router.post(
    "/topology/links/{link_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def topology_link_update(request: Request, link_id: str, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    data = web_topology_service.parse_link_form(form)
    try:
        web_topology_service.update_link(db, link_id, data=data)
        return RedirectResponse(url="/admin/network/topology", status_code=303)
    except (ValueError, Exception) as exc:
        context = _base_context(request, db, active_page="topology")
        context.update(
            web_topology_service.link_form_context(
                db,
                link=data,
                action_url=f"/admin/network/topology/links/{link_id}/edit",
                error=str(exc),
            )
        )
        return templates.TemplateResponse(
            "admin/network/topology/link_form.html", context
        )


@router.post(
    "/topology/links/{link_id}/delete",
    dependencies=[Depends(require_permission("network:write"))],
)
def topology_link_delete(
    link_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    web_topology_service.delete_link(db, link_id)
    return RedirectResponse(url="/admin/network/topology", status_code=303)


# ── AJAX Helpers ─────────────────────────────────────────────────────


@router.get(
    "/topology/api/interfaces/{device_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def topology_device_interfaces(device_id: str, db: Session = Depends(get_db)):
    """Return interfaces for a device (populates dropdowns via HTMX/JS)."""
    return web_topology_service.get_device_interfaces(db, device_id)


@router.get(
    "/topology/api/node/{device_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def topology_node_summary(device_id: str, db: Session = Depends(get_db)):
    """Return node summary for drilldown panel."""
    return web_topology_service.node_summary(db, device_id)


@router.get(
    "/topology/api/graph",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def topology_graph_data(
    group: str | None = None, site: str | None = None, db: Session = Depends(get_db)
):
    """Return full graph data as JSON (for D3 refresh without full page reload)."""
    return web_topology_service.graph_data(db, group=group, site=site)
