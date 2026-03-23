"""Admin ONT provisioning profile catalog web routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_ont_provisioning_profiles as web_profile_service
from app.services.auth_dependencies import require_permission
from app.services.network.ont_provisioning_profiles import (
    ont_provisioning_profiles,
    wan_services,
)
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network/provisioning-profiles",
    tags=["web-admin-provisioning-profiles"],
)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def provisioning_profiles_list(
    request: Request,
    search: str | None = None,
    profile_type: str | None = None,
    config_method: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all ONT provisioning profiles."""
    context = web_profile_service.list_context(
        request,
        db,
        search=search,
        profile_type=profile_type,
        config_method=config_method,
    )
    return templates.TemplateResponse(
        "admin/network/provisioning-profiles/index.html", context
    )


@router.get(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def provisioning_profile_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show provisioning profile create form."""
    context = web_profile_service.form_context(request, db)
    return templates.TemplateResponse(
        "admin/network/provisioning-profiles/form.html", context
    )


@router.post(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def provisioning_profile_create(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Create a new provisioning profile."""
    form = parse_form_data_sync(request)
    values = web_profile_service.parse_profile_form(form)
    error = web_profile_service.validate_profile_form(values)
    if error:
        context = web_profile_service.form_context(request, db)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/provisioning-profiles/form.html", context
        )

    web_profile_service.handle_create(request, db, values)
    return RedirectResponse("/admin/network/provisioning-profiles", status_code=303)


@router.get(
    "/{profile_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def provisioning_profile_edit_form(
    request: Request,
    profile_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show provisioning profile edit form."""
    try:
        context = web_profile_service.form_context(request, db, profile_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Provisioning profile not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/network/provisioning-profiles/form.html", context
    )


@router.post(
    "/{profile_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def provisioning_profile_update(
    request: Request,
    profile_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Update an existing provisioning profile."""
    form = parse_form_data_sync(request)
    values = web_profile_service.parse_profile_form(form)
    error = web_profile_service.validate_profile_form(values)
    if error:
        context = web_profile_service.form_context(request, db, profile_id)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/provisioning-profiles/form.html", context
        )

    web_profile_service.handle_update(request, db, profile_id, values)
    return RedirectResponse("/admin/network/provisioning-profiles", status_code=303)


@router.post(
    "/{profile_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def provisioning_profile_delete(
    request: Request,
    profile_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Soft-delete a provisioning profile."""
    ont_provisioning_profiles.delete(db, profile_id)
    return RedirectResponse("/admin/network/provisioning-profiles", status_code=303)


# ── WAN Service sub-routes ──


@router.post(
    "/{profile_id}/wan-services/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def wan_service_create(
    request: Request,
    profile_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Create a WAN service for a profile."""
    form = parse_form_data_sync(request)
    values = web_profile_service.parse_wan_service_form(form)
    error = web_profile_service.validate_wan_service_form(values)
    if error:
        context = web_profile_service.form_context(request, db, profile_id)
        context["wan_error"] = error
        context["wan_form_values"] = values
        return templates.TemplateResponse(
            "admin/network/provisioning-profiles/form.html", context
        )

    web_profile_service.handle_wan_service_create(db, profile_id, values)
    return RedirectResponse(
        f"/admin/network/provisioning-profiles/{profile_id}/edit", status_code=303
    )


@router.post(
    "/{profile_id}/wan-services/{service_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def wan_service_delete(
    request: Request,
    profile_id: str,
    service_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Delete a WAN service from a profile."""
    wan_services.delete(db, service_id)
    return RedirectResponse(
        f"/admin/network/provisioning-profiles/{profile_id}/edit", status_code=303
    )
