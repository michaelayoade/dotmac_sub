"""Admin authorization preset web routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_authorization_presets as web_preset_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync
from app.web.templates import templates

router = APIRouter(
    prefix="/network/authorization-presets",
    tags=["web-admin-authorization-presets"],
)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def authorization_presets_list(
    request: Request,
    search: str | None = None,
    olt_device_id: str | None = None,
    is_active: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all authorization presets."""
    context = web_preset_service.list_context(
        request,
        db,
        search=search,
        olt_device_id=olt_device_id,
        is_active=is_active,
    )
    return templates.TemplateResponse(
        "admin/network/authorization-presets/index.html", context
    )


@router.get(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def authorization_preset_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show authorization preset create form."""
    context = web_preset_service.form_context(request, db)
    return templates.TemplateResponse(
        "admin/network/authorization-presets/form.html", context
    )


@router.post(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def authorization_preset_create(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Create a new authorization preset."""
    form = parse_form_data_sync(request)
    values = web_preset_service.parse_preset_form(form)
    error = web_preset_service.validate_preset_form(values)
    if error:
        context = web_preset_service.form_context(request, db)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/authorization-presets/form.html", context
        )

    try:
        web_preset_service.handle_create(request, db, values)
    except Exception as exc:
        context = web_preset_service.form_context(request, db)
        context["error"] = str(exc)
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/authorization-presets/form.html", context, status_code=400
        )
    return RedirectResponse("/admin/network/authorization-presets", status_code=303)


@router.get(
    "/{preset_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def authorization_preset_edit_form(
    request: Request,
    preset_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show authorization preset edit form."""
    try:
        context = web_preset_service.form_context(request, db, preset_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Authorization preset not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/network/authorization-presets/form.html", context
    )


@router.post(
    "/{preset_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def authorization_preset_update(
    request: Request,
    preset_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Update an existing authorization preset."""
    form = parse_form_data_sync(request)
    values = web_preset_service.parse_preset_form(form)
    error = web_preset_service.validate_preset_form(values)
    if error:
        context = web_preset_service.form_context(request, db, preset_id)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/authorization-presets/form.html", context
        )

    try:
        web_preset_service.handle_update(request, db, preset_id, values)
    except Exception as exc:
        context = web_preset_service.form_context(request, db, preset_id)
        context["error"] = str(exc)
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/authorization-presets/form.html", context, status_code=400
        )
    return RedirectResponse("/admin/network/authorization-presets", status_code=303)


@router.post(
    "/{preset_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def authorization_preset_delete(
    request: Request,
    preset_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Delete an authorization preset."""
    web_preset_service.handle_delete(db, preset_id)
    return RedirectResponse("/admin/network/authorization-presets", status_code=303)
