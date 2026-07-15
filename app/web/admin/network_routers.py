"""Admin web routes for router management."""

import json
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.router_management import RouterUpdate
from app.services import web_network_routers as web_routers_service
from app.services.auth_dependencies import require_permission
from app.services.router_management.connection import RouterConnectionService
from app.services.router_management.inventory import RouterInventory
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network/routers",
    tags=["web-admin-routers"],
    dependencies=[Depends(require_permission("router:read"))],
)


def _toast_response(message: str, toast_type: str, status_code: int = 204) -> Response:
    return Response(
        status_code=status_code,
        headers={
            "HX-Trigger": json.dumps(
                {"showToast": {"message": message, "type": toast_type}},
                ensure_ascii=True,
            )
        },
    )


@router.get("", response_class=HTMLResponse)
def router_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    offset = (page - 1) * limit
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


@router.get(
    "/templates/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("router:write"))],
)
def template_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.template_form_context(request, db)
    return templates.TemplateResponse(
        "admin/network/routers/templates/form.html", context
    )


@router.post(
    "/templates/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("router:write"))],
)
def template_create(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_routers_service.create_template(db, parse_form_data_sync(request))
    return RedirectResponse(url="/admin/network/routers/templates", status_code=303)


@router.get(
    "/templates/{template_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("router:write"))],
)
def template_edit_form(
    request: Request,
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = web_routers_service.template_form_context(
        request, db, template_id=template_id
    )
    return templates.TemplateResponse(
        "admin/network/routers/templates/form.html", context
    )


@router.post(
    "/templates/{template_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("router:write"))],
)
def template_update(
    request: Request,
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_routers_service.update_template(db, template_id, parse_form_data_sync(request))
    return RedirectResponse(url="/admin/network/routers/templates", status_code=303)


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


@router.post(
    "/{router_id}/test-connection",
    dependencies=[Depends(require_permission("router:read"))],
)
def router_test_connection(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    router_model = RouterInventory.get(db, router_id)
    result = RouterConnectionService.test_connection(router_model)
    return _toast_response(
        result.message,
        "success" if result.success else "error",
        status_code=204 if result.success else 502,
    )


@router.post(
    "/{router_id}/sync",
    dependencies=[Depends(require_permission("router:write"))],
)
def router_sync_now(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    router_model = RouterInventory.get(db, router_id)
    try:
        RouterInventory.sync_system_info(db, router_model)
        RouterInventory.sync_interfaces(db, router_model)
    except Exception as exc:
        RouterInventory.update(db, router_model.id, RouterUpdate(status="unreachable"))
        return _toast_response(f"Router sync failed: {exc}", "error", status_code=502)
    version = router_model.routeros_version or "unknown version"
    return _toast_response(f"Router sync complete ({version}).", "success")


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
