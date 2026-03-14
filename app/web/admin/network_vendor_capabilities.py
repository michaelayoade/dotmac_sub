"""Admin vendor model capability web routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_vendor_capabilities as web_vc_service
from app.services.auth_dependencies import require_permission
from app.services.network.vendor_capabilities import (
    tr069_parameter_maps,
    vendor_capabilities,
)
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network/vendor-capabilities", tags=["web-admin-vendor-capabilities"]
)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def vendor_capabilities_list(
    request: Request,
    search: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all vendor model capabilities."""
    context = web_vc_service.list_context(request, db, search=search, vendor=vendor)
    return templates.TemplateResponse(
        "admin/network/vendor-capabilities/index.html", context
    )


@router.get(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def vendor_capability_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show vendor capability create form."""
    context = web_vc_service.form_context(request, db)
    return templates.TemplateResponse(
        "admin/network/vendor-capabilities/form.html", context
    )


@router.post(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def vendor_capability_create(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Create a new vendor model capability."""
    form = parse_form_data_sync(request)
    values = web_vc_service.parse_capability_form(form)
    error = web_vc_service.validate_capability_form(values)
    if error:
        context = web_vc_service.form_context(request, db)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/vendor-capabilities/form.html", context
        )

    web_vc_service.handle_create(db, values)
    return RedirectResponse("/admin/network/vendor-capabilities", status_code=303)


@router.get(
    "/{capability_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def vendor_capability_edit_form(
    request: Request,
    capability_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show vendor capability edit form."""
    try:
        context = web_vc_service.form_context(request, db, capability_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Vendor capability not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/network/vendor-capabilities/form.html", context
    )


@router.post(
    "/{capability_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def vendor_capability_update(
    request: Request,
    capability_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Update an existing vendor model capability."""
    form = parse_form_data_sync(request)
    values = web_vc_service.parse_capability_form(form)
    error = web_vc_service.validate_capability_form(values)
    if error:
        context = web_vc_service.form_context(request, db, capability_id)
        context["error"] = error
        context["form_values"] = values
        return templates.TemplateResponse(
            "admin/network/vendor-capabilities/form.html", context
        )

    web_vc_service.handle_update(db, capability_id, values)
    return RedirectResponse("/admin/network/vendor-capabilities", status_code=303)


@router.post(
    "/{capability_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def vendor_capability_delete(
    request: Request,
    capability_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Soft-delete a vendor model capability."""
    vendor_capabilities.delete(db, capability_id)
    return RedirectResponse("/admin/network/vendor-capabilities", status_code=303)


# -- TR-069 Parameter Map sub-routes --


@router.post(
    "/{capability_id}/params/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def param_map_create(
    request: Request,
    capability_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Create a TR-069 parameter map entry."""
    form = parse_form_data_sync(request)
    values = web_vc_service.parse_param_map_form(form)
    error = web_vc_service.validate_param_map_form(values)
    if error:
        context = web_vc_service.form_context(request, db, capability_id)
        context["param_error"] = error
        context["param_form_values"] = values
        return templates.TemplateResponse(
            "admin/network/vendor-capabilities/form.html", context
        )

    web_vc_service.handle_param_map_create(db, capability_id, values)
    return RedirectResponse(
        f"/admin/network/vendor-capabilities/{capability_id}/edit", status_code=303
    )


@router.post(
    "/{capability_id}/params/{param_map_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def param_map_delete(
    request: Request,
    capability_id: str,
    param_map_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Delete a TR-069 parameter map entry."""
    tr069_parameter_maps.delete(db, param_map_id)
    return RedirectResponse(
        f"/admin/network/vendor-capabilities/{capability_id}/edit", status_code=303
    )
