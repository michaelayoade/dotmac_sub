"""Web helpers for admin TR-069 provision routes."""

from __future__ import annotations

from urllib.parse import quote_plus

from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.models.tr069 import Tr069AcsServer
from app.services import tr069 as tr069_service
from app.services.tr069_provisions import provisions as provisions_service


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

    provisions: list[object] = []
    error = None
    if selected:
        try:
            provisions = list(provisions_service.list(db, selected))
        except Exception as exc:
            error = str(exc)

    return {
        "request": request,
        "active_page": "tr069-provisions",
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "servers": servers,
        "selected_server_id": selected or "",
        "provisions": provisions,
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
        "active_page": "tr069-provisions",
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "servers": servers,
        "selected_server_id": selected or "",
        "provision": None,
        "is_edit": False,
        "error": None,
    }


def edit_context(
    request: Request,
    db: Session,
    *,
    provision_id: str,
    acs_server_id: str | None = None,
) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    servers = _servers(db)
    selected = _selected_server_id(servers, acs_server_id)

    provision = None
    error = None
    if selected:
        try:
            provision = provisions_service.get(db, selected, provision_id)
        except Exception as exc:
            error = str(exc)

    return {
        "request": request,
        "active_page": "tr069-provisions",
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "servers": servers,
        "selected_server_id": selected or "",
        "provision": provision,
        "provision_id": provision_id,
        "is_edit": True,
        "error": error,
    }


def redirect_url(
    *,
    acs_server_id: str,
    status: str,
    message: str,
) -> str:
    return (
        "/admin/network/tr069/provisions"
        f"?acs_server_id={acs_server_id}&status={status}&message={quote_plus(message)}"
    )


def create_provision(db: Session, request: Request, form: FormData) -> str:
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

        provisions_service.create(
            db, acs_server_id, provision_id, script, request=request
        )
        return redirect_url(
            acs_server_id=acs_server_id,
            status="success",
            message=f"Provision '{provision_id}' created",
        )
    except Exception as exc:
        return redirect_url(
            acs_server_id=acs_server_id,
            status="error",
            message=str(exc),
        )


def update_provision(
    db: Session,
    request: Request,
    *,
    provision_id: str,
    form: FormData,
) -> str:
    acs_server_id = str(form.get("acs_server_id") or "").strip()
    script = str(form.get("script") or "").strip()

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")
        if not script:
            raise ValueError("Provision script is required")

        provisions_service.update(
            db, acs_server_id, provision_id, script, request=request
        )
        return redirect_url(
            acs_server_id=acs_server_id,
            status="success",
            message=f"Provision '{provision_id}' updated",
        )
    except Exception as exc:
        return redirect_url(
            acs_server_id=acs_server_id,
            status="error",
            message=str(exc),
        )


def delete_provision(
    db: Session,
    request: Request,
    *,
    provision_id: str,
    form: FormData,
) -> str:
    acs_server_id = str(form.get("acs_server_id") or "").strip()

    try:
        if not acs_server_id:
            raise ValueError("ACS server is required")

        provisions_service.delete(db, acs_server_id, provision_id, request=request)
        return redirect_url(
            acs_server_id=acs_server_id,
            status="success",
            message=f"Provision '{provision_id}' deleted",
        )
    except Exception as exc:
        return redirect_url(
            acs_server_id=acs_server_id,
            status="error",
            message=str(exc),
        )
