"""Admin TR-069 provision management web routes."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import tr069 as tr069_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_method_permission
from app.services.tr069_provisions import provisions as provisions_service
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network",
    tags=["web-admin-network-tr069-provisions"],
    dependencies=[
        Depends(require_method_permission("network:tr069:read", "network:tr069:write"))
    ],
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


@router.get("/tr069/provisions", response_class=HTMLResponse)
def list_provisions(
    request: Request,
    acs_server_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all provisions for the selected ACS server."""
    servers = tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    selected_server_id = str(acs_server_id or "").strip() or None
    if not selected_server_id and servers:
        selected_server_id = str(servers[0].id)

    provisions_list: list = []
    error = None
    if selected_server_id:
        try:
            provisions_list = provisions_service.list(db, selected_server_id)
        except Exception as e:
            error = str(e)

    context = _base_context(request, db, active_page="tr069-provisions")
    context.update({
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "provisions": provisions_list,
        "error": error,
        "status": status,
        "message": message,
    })
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
    servers = tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    selected_server_id = str(acs_server_id or "").strip() or None
    if not selected_server_id and servers:
        selected_server_id = str(servers[0].id)

    context = _base_context(request, db, active_page="tr069-provisions")
    context.update({
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "provision": None,
        "is_edit": False,
        "error": None,
    })
    return templates.TemplateResponse(
        "admin/network/tr069/provisions/form.html", context
    )


@router.post("/tr069/provisions", response_class=HTMLResponse)
def create_provision(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create a new provision."""
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    provision_id = str(form.get("provision_id") or "").strip()
    script = str(form.get("script") or "").strip()

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")
        if not provision_id:
            raise ValueError("Provision ID is required")
        if not script:
            raise ValueError("Provision script is required")

        provisions_service.create(db, acs_server_id, provision_id, script)

        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="tr069_provision",
            entity_id=provision_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"acs_server_id": acs_server_id},
        )

        message = quote_plus(f"Provision '{provision_id}' created")
        return RedirectResponse(
            f"/admin/network/tr069/provisions?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/provisions?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


@router.get("/tr069/provisions/{provision_id}/edit", response_class=HTMLResponse)
def edit_provision(
    request: Request,
    provision_id: str,
    acs_server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show form to edit an existing provision."""
    servers = tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    selected_server_id = str(acs_server_id or "").strip() or None
    if not selected_server_id and servers:
        selected_server_id = str(servers[0].id)

    provision = None
    error = None
    if selected_server_id:
        try:
            provision = provisions_service.get(db, selected_server_id, provision_id)
        except Exception as e:
            error = str(e)

    context = _base_context(request, db, active_page="tr069-provisions")
    context.update({
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "provision": provision,
        "provision_id": provision_id,
        "is_edit": True,
        "error": error,
    })
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
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    script = str(form.get("script") or "").strip()

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")
        if not script:
            raise ValueError("Provision script is required")

        provisions_service.update(db, acs_server_id, provision_id, script)

        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="tr069_provision",
            entity_id=provision_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"acs_server_id": acs_server_id},
        )

        message = quote_plus(f"Provision '{provision_id}' updated")
        return RedirectResponse(
            f"/admin/network/tr069/provisions?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/provisions?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


@router.post("/tr069/provisions/{provision_id}/delete", response_class=HTMLResponse)
def delete_provision(
    request: Request,
    provision_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Delete a provision."""
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip()

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")

        provisions_service.delete(db, acs_server_id, provision_id)

        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="tr069_provision",
            entity_id=provision_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"acs_server_id": acs_server_id},
        )

        message = quote_plus(f"Provision '{provision_id}' deleted")
        return RedirectResponse(
            f"/admin/network/tr069/provisions?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/provisions?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )
