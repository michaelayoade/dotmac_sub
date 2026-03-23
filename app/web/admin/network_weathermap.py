"""Admin network topology routes (replaces legacy weathermap)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import network_topology as topology_service
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
    graph = topology_service.list_nodes_and_edges(
        db,
        topology_group=group,
        pop_site_id=site,
        include_utilization=True,
    )
    form_options = topology_service.get_form_options(db)
    context = _base_context(request, db, active_page="topology")
    context.update(
        {
            "graph": graph,
            "form_options": form_options,
            "selected_group": group or "",
            "selected_site": site or "",
        }
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
    form_options = topology_service.get_form_options(db)
    context = _base_context(request, db, active_page="topology")
    context.update(
        {
            "link": None,
            "action_url": "/admin/network/topology/links/new",
            "error": None,
            **form_options,
        }
    )
    return templates.TemplateResponse("admin/network/topology/link_form.html", context)


@router.post(
    "/topology/links/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def topology_link_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    data = _parse_link_form(form)
    try:
        topology_service.topology_links.create(db, data=data)
        return RedirectResponse(url="/admin/network/topology", status_code=303)
    except (ValueError, Exception) as exc:
        form_options = topology_service.get_form_options(db)
        context = _base_context(request, db, active_page="topology")
        context.update(
            {
                "link": data,
                "action_url": "/admin/network/topology/links/new",
                "error": str(exc),
                **form_options,
            }
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
    link = topology_service.topology_links.get(db, link_id)
    form_options = topology_service.get_form_options(db)
    context = _base_context(request, db, active_page="topology")
    context.update(
        {
            "link": link,
            "action_url": f"/admin/network/topology/links/{link_id}/edit",
            "error": None,
            **form_options,
        }
    )
    return templates.TemplateResponse("admin/network/topology/link_form.html", context)


@router.post(
    "/topology/links/{link_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def topology_link_update(request: Request, link_id: str, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    data = _parse_link_form(form)
    try:
        topology_service.topology_links.update(db, link_id, data=data)
        return RedirectResponse(url="/admin/network/topology", status_code=303)
    except (ValueError, Exception) as exc:
        link = topology_service.topology_links.get(db, link_id)
        form_options = topology_service.get_form_options(db)
        context = _base_context(request, db, active_page="topology")
        context.update(
            {
                "link": link,
                "action_url": f"/admin/network/topology/links/{link_id}/edit",
                "error": str(exc),
                **form_options,
            }
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
    topology_service.topology_links.delete(db, link_id)
    return RedirectResponse(url="/admin/network/topology", status_code=303)


# ── AJAX Helpers ─────────────────────────────────────────────────────


@router.get(
    "/topology/api/interfaces/{device_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def topology_device_interfaces(device_id: str, db: Session = Depends(get_db)):
    """Return interfaces for a device (populates dropdowns via HTMX/JS)."""
    return topology_service.get_device_interfaces(db, device_id)


@router.get(
    "/topology/api/node/{device_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def topology_node_summary(device_id: str, db: Session = Depends(get_db)):
    """Return node summary for drilldown panel."""
    return topology_service.node_summary(db, device_id)


@router.get(
    "/topology/api/graph",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def topology_graph_data(
    group: str | None = None, site: str | None = None, db: Session = Depends(get_db)
):
    """Return full graph data as JSON (for D3 refresh without full page reload)."""
    return topology_service.list_nodes_and_edges(
        db, topology_group=group, pop_site_id=site
    )


def _parse_link_form(form) -> dict:
    return {
        "source_device_id": str(form.get("source_device_id") or "").strip(),
        "source_interface_id": str(form.get("source_interface_id") or "").strip()
        or None,
        "target_device_id": str(form.get("target_device_id") or "").strip(),
        "target_interface_id": str(form.get("target_interface_id") or "").strip()
        or None,
        "link_role": str(form.get("link_role") or "unknown").strip(),
        "medium": str(form.get("medium") or "unknown").strip(),
        "capacity_bps": str(form.get("capacity_bps") or "").strip() or None,
        "bundle_key": str(form.get("bundle_key") or "").strip() or None,
        "topology_group": str(form.get("topology_group") or "").strip() or None,
        "admin_status": str(form.get("admin_status") or "enabled").strip(),
        "notes": str(form.get("notes") or "").strip() or None,
    }
