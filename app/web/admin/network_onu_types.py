"""Admin ONU type catalog web routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_onu_types as web_onu_types_service
from app.services.auth_dependencies import require_permission
from app.services.network.onu_types import onu_types
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network/onu-types", tags=["web-admin-onu-types"])


def _base_context(
    request: Request,
    db: Session,
    active_page: str = "onu-types",
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


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def onu_types_list(
    request: Request,
    search: str | None = None,
    pon_type: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all ONU types."""
    context = web_onu_types_service.list_context(
        request, db, search=search, pon_type=pon_type
    )
    return templates.TemplateResponse("admin/network/onu-types/index.html", context)


@router.get(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def onu_type_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show ONU type create form."""
    context = web_onu_types_service.form_context(request, db)
    return templates.TemplateResponse("admin/network/onu-types/form.html", context)


@router.post(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def onu_type_create(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Create a new ONU type."""
    form = parse_form_data_sync(request)
    values = web_onu_types_service.parse_form_values(form)
    error = web_onu_types_service.validate_form(values)
    if error:
        context = web_onu_types_service.form_context(request, db)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse("admin/network/onu-types/form.html", context)

    web_onu_types_service.handle_create(db, values)
    return RedirectResponse("/admin/network/onu-types", status_code=303)


@router.get(
    "/{onu_type_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def onu_type_edit_form(
    request: Request,
    onu_type_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show ONU type edit form."""
    try:
        context = web_onu_types_service.form_context(request, db, onu_type_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONU type not found"},
            status_code=404,
        )
    return templates.TemplateResponse("admin/network/onu-types/form.html", context)


@router.post(
    "/{onu_type_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def onu_type_update(
    request: Request,
    onu_type_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Update an existing ONU type."""
    form = parse_form_data_sync(request)
    values = web_onu_types_service.parse_form_values(form)
    error = web_onu_types_service.validate_form(values)
    if error:
        context = web_onu_types_service.form_context(request, db, onu_type_id)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse("admin/network/onu-types/form.html", context)

    web_onu_types_service.handle_update(db, onu_type_id, values)
    return RedirectResponse("/admin/network/onu-types", status_code=303)


@router.post(
    "/{onu_type_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def onu_type_delete(
    request: Request,
    onu_type_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Soft-delete an ONU type."""
    onu_types.delete(db, onu_type_id)
    return RedirectResponse("/admin/network/onu-types", status_code=303)
