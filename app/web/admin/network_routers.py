"""Admin web routes for router management."""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_routers as web_routers_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network/routers",
    tags=["web-admin-routers"],
    dependencies=[Depends(require_permission("router:read"))],
)


@router.get("", response_class=HTMLResponse)
def router_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.list_context(
        request,
        db,
        status=status,
        search=search,
        limit=limit,
        offset=offset,
    )
    return templates.TemplateResponse("admin/network/routers/index.html", context)


@router.get("/dashboard", response_class=HTMLResponse)
def router_dashboard(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.dashboard_context(request, db)
    return templates.TemplateResponse("admin/network/routers/dashboard.html", context)


@router.get("/new", response_class=HTMLResponse)
def router_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.create_form_context(request, db)
    return templates.TemplateResponse("admin/network/routers/form.html", context)


@router.post("/new", response_class=HTMLResponse)
def router_create(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    r = web_routers_service.create_router(db, parse_form_data_sync(request))
    return RedirectResponse(url=f"/admin/network/routers/{r.id}", status_code=303)


@router.get("/templates", response_class=HTMLResponse)
def template_list(
    request: Request,
    category: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.template_list_context(
        request,
        db,
        category=category,
    )
    return templates.TemplateResponse(
        "admin/network/routers/templates/index.html", context
    )


@router.get("/templates/new", response_class=HTMLResponse)
def template_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.template_form_context(request, db)
    return templates.TemplateResponse(
        "admin/network/routers/templates/form.html", context
    )


@router.get("/push", response_class=HTMLResponse)
def push_wizard(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.push_wizard_context(request, db)
    return templates.TemplateResponse("admin/network/routers/push.html", context)


@router.get("/push/{push_id}", response_class=HTMLResponse)
def push_detail(
    request: Request,
    push_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.push_detail_context(request, db, push_id=push_id)
    return templates.TemplateResponse("admin/network/routers/push_detail.html", context)


@router.get("/jump-hosts", response_class=HTMLResponse)
def jump_host_list(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.jump_host_list_context(request, db)
    return templates.TemplateResponse("admin/network/routers/jump_hosts.html", context)


@router.get("/{router_id}", response_class=HTMLResponse)
def router_detail(
    request: Request,
    router_id: uuid.UUID,
    tab: str = "overview",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.detail_context(
        request,
        db,
        router_id=router_id,
        tab=tab,
    )
    return templates.TemplateResponse("admin/network/routers/detail.html", context)


@router.get("/{router_id}/edit", response_class=HTMLResponse)
def router_edit_form(
    request: Request,
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.edit_form_context(request, db, router_id=router_id)
    return templates.TemplateResponse("admin/network/routers/form.html", context)


@router.post("/{router_id}/edit", response_class=HTMLResponse)
def router_edit(
    request: Request,
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_routers_service.update_router(db, router_id, parse_form_data_sync(request))
    return RedirectResponse(url=f"/admin/network/routers/{router_id}", status_code=303)
