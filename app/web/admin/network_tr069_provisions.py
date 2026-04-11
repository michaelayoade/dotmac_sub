"""Admin TR-069 provision management web routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_tr069_provisions as web_provisions_service
from app.services.auth_dependencies import require_method_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network",
    tags=["web-admin-network-tr069-provisions"],
    dependencies=[
        Depends(require_method_permission("network:tr069:read", "network:tr069:write"))
    ],
)


@router.get("/tr069/provisions", response_class=HTMLResponse)
def list_provisions(
    request: Request,
    acs_server_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all provisions for the selected ACS server."""
    context = web_provisions_service.list_context(
        request,
        db,
        acs_server_id=acs_server_id,
        status=status,
        message=message,
    )
    return templates.TemplateResponse(
        "admin/network/tr069/provisions/index.html", context
    )


@router.get("/tr069/provisions/new", response_class=HTMLResponse)
def new_provision(
    request: Request,
    acs_server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show form to create a new provision."""
    context = web_provisions_service.new_context(
        request,
        db,
        acs_server_id=acs_server_id,
    )
    return templates.TemplateResponse(
        "admin/network/tr069/provisions/form.html", context
    )


@router.post("/tr069/provisions", response_class=HTMLResponse)
def create_provision(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create a new provision."""
    url = web_provisions_service.create_provision(
        db,
        request,
        parse_form_data_sync(request),
    )
    return RedirectResponse(url, status_code=303)


@router.get("/tr069/provisions/{provision_id}/edit", response_class=HTMLResponse)
def edit_provision(
    request: Request,
    provision_id: str,
    acs_server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show form to edit an existing provision."""
    context = web_provisions_service.edit_context(
        request,
        db,
        provision_id=provision_id,
        acs_server_id=acs_server_id,
    )
    return templates.TemplateResponse(
        "admin/network/tr069/provisions/form.html", context
    )


@router.post("/tr069/provisions/{provision_id}", response_class=HTMLResponse)
def update_provision(
    request: Request,
    provision_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Update an existing provision."""
    url = web_provisions_service.update_provision(
        db,
        request,
        provision_id=provision_id,
        form=parse_form_data_sync(request),
    )
    return RedirectResponse(url, status_code=303)


@router.post("/tr069/provisions/{provision_id}/delete", response_class=HTMLResponse)
def delete_provision(
    request: Request,
    provision_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Delete a provision."""
    url = web_provisions_service.delete_provision(
        db,
        request,
        provision_id=provision_id,
        form=parse_form_data_sync(request),
    )
    return RedirectResponse(url, status_code=303)
