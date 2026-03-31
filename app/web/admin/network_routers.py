"""Admin web routes for router management."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_permission
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)
from app.services.router_management.inventory import JumpHostInventory, RouterInventory
from app.services.router_management.monitoring import RouterMonitoringService
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network/routers",
    tags=["web-admin-routers"],
    dependencies=[Depends(require_permission("router:read"))],
)


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


@router.get("", response_class=HTMLResponse)
def router_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["routers"] = RouterInventory.list(
        db, status=status, search=search, limit=limit, offset=offset
    )
    context["status_filter"] = status
    context["search"] = search or ""
    context["summary"] = RouterMonitoringService.get_dashboard_summary(db)
    return templates.TemplateResponse("admin/network/routers/index.html", context)


@router.get("/dashboard", response_class=HTMLResponse)
def router_dashboard(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["summary"] = RouterMonitoringService.get_dashboard_summary(db)
    context["recent_pushes"] = RouterConfigService.list_pushes(db, limit=10)
    return templates.TemplateResponse("admin/network/routers/dashboard.html", context)


@router.get("/new", response_class=HTMLResponse)
def router_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["jump_hosts"] = JumpHostInventory.list(db)
    context["router"] = None
    return templates.TemplateResponse("admin/network/routers/form.html", context)


@router.post("/new", response_class=HTMLResponse)
def router_create(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.schemas.router_management import RouterCreate

    form_data = parse_form_data_sync(request)
    data: dict[str, Any] = {k: v for k, v in form_data.items() if isinstance(v, str)}
    payload = RouterCreate(**data)
    r = RouterInventory.create(db, payload)
    return RedirectResponse(url=f"/admin/network/routers/{r.id}", status_code=303)


@router.get("/templates", response_class=HTMLResponse)
def template_list(
    request: Request,
    category: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["templates"] = RouterTemplateService.list(db, category=category)
    context["category_filter"] = category
    return templates.TemplateResponse(
        "admin/network/routers/templates/index.html", context
    )


@router.get("/templates/new", response_class=HTMLResponse)
def template_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["template"] = None
    return templates.TemplateResponse(
        "admin/network/routers/templates/form.html", context
    )


@router.get("/push", response_class=HTMLResponse)
def push_wizard(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["routers"] = RouterInventory.list(db, limit=200)
    context["templates"] = RouterTemplateService.list(db)
    return templates.TemplateResponse("admin/network/routers/push.html", context)


@router.get("/push/{push_id}", response_class=HTMLResponse)
def push_detail(
    request: Request,
    push_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    push = RouterConfigService.get_push(db, push_id)
    context["push"] = push
    context["results"] = push.results
    return templates.TemplateResponse("admin/network/routers/push_detail.html", context)


@router.get("/jump-hosts", response_class=HTMLResponse)
def jump_host_list(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["jump_hosts"] = JumpHostInventory.list(db)
    return templates.TemplateResponse("admin/network/routers/jump_hosts.html", context)


@router.get("/{router_id}", response_class=HTMLResponse)
def router_detail(
    request: Request,
    router_id: uuid.UUID,
    tab: str = "overview",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    r = RouterInventory.get(db, router_id)
    context["router"] = r
    context["tab"] = tab

    if tab == "interfaces":
        context["interfaces"] = RouterInventory.list_interfaces(db, router_id)
    elif tab == "config":
        context["snapshots"] = RouterConfigService.list_snapshots(
            db, router_id, limit=20
        )
    elif tab == "pushes":
        context["push_results"] = RouterConfigService.list_push_results(
            db, router_id, limit=20
        )

    return templates.TemplateResponse("admin/network/routers/detail.html", context)


@router.get("/{router_id}/edit", response_class=HTMLResponse)
def router_edit_form(
    request: Request,
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, "routers")
    context["router"] = RouterInventory.get(db, router_id)
    context["jump_hosts"] = JumpHostInventory.list(db)
    return templates.TemplateResponse("admin/network/routers/form.html", context)


@router.post("/{router_id}/edit", response_class=HTMLResponse)
def router_edit(
    request: Request,
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.schemas.router_management import RouterUpdate

    form_data = parse_form_data_sync(request)
    data: dict[str, Any] = {k: v for k, v in form_data.items() if isinstance(v, str)}
    payload = RouterUpdate(**data)
    RouterInventory.update(db, router_id, payload)
    return RedirectResponse(url=f"/admin/network/routers/{router_id}", status_code=303)
