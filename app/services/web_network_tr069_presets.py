"""Web helpers for admin TR-069 preset routes."""

from __future__ import annotations

import json
from urllib.parse import quote_plus

from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.models.tr069 import Tr069AcsServer
from app.services import tr069 as tr069_service
from app.services.tr069_presets import presets as presets_service


def _servers(db: Session) -> list[Tr069AcsServer]:
    return tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )


def _selected_server_id(
    servers: list[Tr069AcsServer], acs_server_id: str | None
) -> str | None:
    selected = str(acs_server_id or "").strip() or None
    if not selected and servers:
        selected = str(servers[0].id)
    return selected


def list_context(
    request: Request,
    db: Session,
    *,
    acs_server_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    servers = _servers(db)
    selected = _selected_server_id(servers, acs_server_id)

    presets: list[object] = []
    error = None
    if selected:
        try:
            presets = list(presets_service.list(db, selected))
        except Exception as exc:
            error = str(exc)

    return {
        "request": request,
        "active_page": "tr069-presets",
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "servers": servers,
        "selected_server_id": selected or "",
        "presets": presets,
        "error": error,
        "status": status,
        "message": message,
    }


def new_context(
    request: Request,
    db: Session,
    *,
    acs_server_id: str | None = None,
) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    servers = _servers(db)
    selected = _selected_server_id(servers, acs_server_id)
    return {
        "request": request,
        "active_page": "tr069-presets",
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "servers": servers,
        "selected_server_id": selected or "",
        "preset": None,
        "is_edit": False,
        "error": None,
    }


def edit_context(
    request: Request,
    db: Session,
    *,
    preset_id: str,
    acs_server_id: str | None = None,
) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    servers = _servers(db)
    selected = _selected_server_id(servers, acs_server_id)

    preset = None
    error = None
    if selected:
        try:
            preset = presets_service.get(db, selected, preset_id)
        except Exception as exc:
            error = str(exc)

    return {
        "request": request,
        "active_page": "tr069-presets",
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "servers": servers,
        "selected_server_id": selected or "",
        "preset": preset,
        "preset_id": preset_id,
        "is_edit": True,
        "error": error,
    }


def _parse_events(events_raw: str) -> dict[str, bool]:
    if not events_raw:
        return {}
    if events_raw.startswith("["):
        return dict.fromkeys(json.loads(events_raw), True)
    return {event.strip(): True for event in events_raw.split(",") if event.strip()}


def _parse_configurations(configurations_raw: str) -> list[object]:
    if not configurations_raw:
        return []
    return json.loads(configurations_raw)


def _preset_data(form: FormData, preset_id: str) -> tuple[str, dict[str, object]]:
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    precondition = str(form.get("precondition") or "").strip()
    events_raw = str(form.get("events") or "").strip()
    configurations_raw = str(form.get("configurations") or "").strip()
    weight = int(str(form.get("weight") or 0))

    if not acs_server_id:
        raise ValueError("ACS server is required")
    if not preset_id:
        raise ValueError("Preset ID is required")

    return acs_server_id, {
        "_id": preset_id,
        "weight": weight,
        "precondition": precondition or "",
        "events": _parse_events(events_raw),
        "configurations": _parse_configurations(configurations_raw),
    }


def redirect_url(
    *,
    acs_server_id: str,
    status: str,
    message: str,
) -> str:
    return (
        "/admin/network/tr069/presets"
        f"?acs_server_id={acs_server_id}&status={status}&message={quote_plus(message)}"
    )


def create_preset(db: Session, request: Request, form: FormData) -> str:
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    preset_id = str(form.get("preset_id") or "").strip()
    try:
        acs_server_id, preset_data = _preset_data(form, preset_id)
        presets_service.create(db, acs_server_id, preset_data, request=request)
        return redirect_url(
            acs_server_id=acs_server_id,
            status="success",
            message=f"Preset '{preset_id}' created",
        )
    except Exception as exc:
        return redirect_url(
            acs_server_id=acs_server_id,
            status="error",
            message=str(exc),
        )


def update_preset(
    db: Session,
    request: Request,
    *,
    preset_id: str,
    form: FormData,
) -> str:
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    try:
        acs_server_id, preset_data = _preset_data(form, preset_id)
        presets_service.update(
            db, acs_server_id, preset_id, preset_data, request=request
        )
        return redirect_url(
            acs_server_id=acs_server_id,
            status="success",
            message=f"Preset '{preset_id}' updated",
        )
    except Exception as exc:
        return redirect_url(
            acs_server_id=acs_server_id,
            status="error",
            message=str(exc),
        )


def delete_preset(
    db: Session,
    request: Request,
    *,
    preset_id: str,
    form: FormData,
) -> str:
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")
        presets_service.delete(db, acs_server_id, preset_id, request=request)
        return redirect_url(
            acs_server_id=acs_server_id,
            status="success",
            message=f"Preset '{preset_id}' deleted",
        )
    except Exception as exc:
        return redirect_url(
            acs_server_id=acs_server_id,
            status="error",
            message=str(exc),
        )
