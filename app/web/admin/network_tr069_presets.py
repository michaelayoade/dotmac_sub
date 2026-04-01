"""Admin TR-069 preset management web routes."""

from __future__ import annotations

import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import tr069 as tr069_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_method_permission
from app.services.tr069_presets import presets as presets_service
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network",
    tags=["web-admin-network-tr069-presets"],
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


@router.get("/tr069/presets", response_class=HTMLResponse)
def list_presets(
    request: Request,
    acs_server_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all presets for the selected ACS server."""
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

    presets_list: list = []
    error = None
    if selected_server_id:
        try:
            presets_list = presets_service.list(db, selected_server_id)
        except Exception as e:
            error = str(e)

    context = _base_context(request, db, active_page="tr069-presets")
    context.update({
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "presets": presets_list,
        "error": error,
        "status": status,
        "message": message,
    })
    return templates.TemplateResponse("admin/network/tr069/presets/index.html", context)


@router.get("/tr069/presets/new", response_class=HTMLResponse)
def new_preset(
    request: Request,
    acs_server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show form to create a new preset."""
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

    context = _base_context(request, db, active_page="tr069-presets")
    context.update({
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "preset": None,
        "is_edit": False,
        "error": None,
    })
    return templates.TemplateResponse("admin/network/tr069/presets/form.html", context)


@router.post("/tr069/presets", response_class=HTMLResponse)
def create_preset(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create a new preset."""
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    preset_id = str(form.get("preset_id") or "").strip()
    precondition = str(form.get("precondition") or "").strip()
    events_raw = str(form.get("events") or "").strip()
    configurations_raw = str(form.get("configurations") or "").strip()
    weight = int(str(form.get("weight") or 0))

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")
        if not preset_id:
            raise ValueError("Preset ID is required")

        # Parse events (comma-separated or JSON array)
        events = {}
        if events_raw:
            if events_raw.startswith("["):
                events = dict.fromkeys(json.loads(events_raw), True)
            else:
                events = {e.strip(): True for e in events_raw.split(",") if e.strip()}

        # Parse configurations (JSON array)
        configurations = []
        if configurations_raw:
            configurations = json.loads(configurations_raw)

        preset_data = {
            "_id": preset_id,
            "weight": weight,
            "precondition": precondition or "",
            "events": events,
            "configurations": configurations,
        }

        presets_service.create(db, acs_server_id, preset_data)

        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="tr069_preset",
            entity_id=preset_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"acs_server_id": acs_server_id},
        )

        message = quote_plus(f"Preset '{preset_id}' created")
        return RedirectResponse(
            f"/admin/network/tr069/presets?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/presets?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


@router.get("/tr069/presets/{preset_id}/edit", response_class=HTMLResponse)
def edit_preset(
    request: Request,
    preset_id: str,
    acs_server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show form to edit an existing preset."""
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

    preset = None
    error = None
    if selected_server_id:
        try:
            preset = presets_service.get(db, selected_server_id, preset_id)
        except Exception as e:
            error = str(e)

    context = _base_context(request, db, active_page="tr069-presets")
    context.update({
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "preset": preset,
        "preset_id": preset_id,
        "is_edit": True,
        "error": error,
    })
    return templates.TemplateResponse("admin/network/tr069/presets/form.html", context)


@router.post("/tr069/presets/{preset_id}", response_class=HTMLResponse)
def update_preset(
    request: Request,
    preset_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Update an existing preset."""
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    precondition = str(form.get("precondition") or "").strip()
    events_raw = str(form.get("events") or "").strip()
    configurations_raw = str(form.get("configurations") or "").strip()
    weight = int(str(form.get("weight") or 0))

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")

        # Parse events
        events = {}
        if events_raw:
            if events_raw.startswith("["):
                events = dict.fromkeys(json.loads(events_raw), True)
            else:
                events = {e.strip(): True for e in events_raw.split(",") if e.strip()}

        # Parse configurations
        configurations = []
        if configurations_raw:
            configurations = json.loads(configurations_raw)

        preset_data = {
            "_id": preset_id,
            "weight": weight,
            "precondition": precondition or "",
            "events": events,
            "configurations": configurations,
        }

        presets_service.update(db, acs_server_id, preset_id, preset_data)

        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="tr069_preset",
            entity_id=preset_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"acs_server_id": acs_server_id},
        )

        message = quote_plus(f"Preset '{preset_id}' updated")
        return RedirectResponse(
            f"/admin/network/tr069/presets?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/presets?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


@router.post("/tr069/presets/{preset_id}/delete", response_class=HTMLResponse)
def delete_preset(
    request: Request,
    preset_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Delete a preset."""
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip()

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")

        presets_service.delete(db, acs_server_id, preset_id)

        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="tr069_preset",
            entity_id=preset_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"acs_server_id": acs_server_id},
        )

        message = quote_plus(f"Preset '{preset_id}' deleted")
        return RedirectResponse(
            f"/admin/network/tr069/presets?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/presets?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )
