"""Admin TR-069 preset management web routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_tr069_presets as web_presets_service
from app.services.auth_dependencies import require_method_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network",
    tags=["web-admin-network-tr069-presets"],
    dependencies=[
        Depends(require_method_permission("network:tr069:read", "network:tr069:write"))
    ],
)


@router.get("/tr069/presets", response_class=HTMLResponse)
def list_presets(
    request: Request,
    acs_server_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all presets for the selected ACS server."""
    context = web_presets_service.list_context(
        request,
        db,
        acs_server_id=acs_server_id,
        status=status,
        message=message,
    )
    return templates.TemplateResponse("admin/network/tr069/presets/index.html", context)


@router.get("/tr069/presets/new", response_class=HTMLResponse)
def new_preset(
    request: Request,
    acs_server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show form to create a new preset."""
    context = web_presets_service.new_context(
        request,
        db,
        acs_server_id=acs_server_id,
    )
    return templates.TemplateResponse("admin/network/tr069/presets/form.html", context)


@router.post("/tr069/presets", response_class=HTMLResponse)
def create_preset(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create a new preset."""
    url = web_presets_service.create_preset(db, request, parse_form_data_sync(request))
    return RedirectResponse(url, status_code=303)


@router.get("/tr069/presets/{preset_id}/edit", response_class=HTMLResponse)
def edit_preset(
    request: Request,
    preset_id: str,
    acs_server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show form to edit an existing preset."""
    context = web_presets_service.edit_context(
        request,
        db,
        preset_id=preset_id,
        acs_server_id=acs_server_id,
    )
    return templates.TemplateResponse("admin/network/tr069/presets/form.html", context)


@router.post("/tr069/presets/{preset_id}", response_class=HTMLResponse)
def update_preset(
    request: Request,
    preset_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Update an existing preset."""
    url = web_presets_service.update_preset(
        db,
        request,
        preset_id=preset_id,
        form=parse_form_data_sync(request),
    )
    return RedirectResponse(url, status_code=303)


@router.post("/tr069/presets/{preset_id}/delete", response_class=HTMLResponse)
def delete_preset(
    request: Request,
    preset_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Delete a preset."""
    url = web_presets_service.delete_preset(
        db,
        request,
        preset_id=preset_id,
        form=parse_form_data_sync(request),
    )
    return RedirectResponse(url, status_code=303)
