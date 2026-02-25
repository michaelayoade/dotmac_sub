"""Admin network RADIUS web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import radius as radius_service
from app.services import web_network_radius as web_network_radius_service
from app.services.audit_helpers import (
    build_audit_activities_for_types,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

RADIUS_CLIENT_EXCLUDE_FIELDS = {"shared_secret_hash"}


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }

@router.get("/radius", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_page(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    state = web_network_radius_service.radius_page_data(db)
    activities = build_audit_activities_for_types(
        db,
        ["radius_server", "radius_client", "radius_profile"],
        limit=10,
    )
    context.update({**state, "activities": activities})
    return templates.TemplateResponse("admin/network/radius/index.html", context)


@router.get("/radius/servers/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_server_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": None,
        "action_url": "/admin/network/radius/servers",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get("/radius/servers", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_servers_redirect():
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.get("/radius/servers/{server_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_server_edit(request: Request, server_id: str, db: Session = Depends(get_db)):
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS server not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server,
        "action_url": f"/admin/network/radius/servers/{server_id}",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post("/radius/servers", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def radius_server_create(request: Request, db: Session = Depends(get_db)):
    values = web_network_radius_service.parse_server_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_server_form(values)
    payload = None
    if not error:
        payload, error = web_network_radius_service.build_server_create_payload(values)
    server_data = web_network_radius_service.server_create_form_data(values)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "server": server_data,
            "action_url": "/admin/network/radius/servers",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/server_form.html", context)

    try:
        assert payload is not None
        server = radius_service.radius_servers.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="radius_server",
            entity_id=str(server.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": server.name, "host": server.host},
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server_data,
        "action_url": "/admin/network/radius/servers",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post("/radius/servers/{server_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def radius_server_update(request: Request, server_id: str, db: Session = Depends(get_db)):
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS server not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    before_snapshot = model_to_dict(server)
    values = web_network_radius_service.parse_server_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_server_form(values)
    payload = None
    if not error:
        payload, error = web_network_radius_service.build_server_payload(
            values,
            current_server=server,
        )
    server_data = web_network_radius_service.server_form_data(
        values,
        current_server=server,
    )

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "server": server_data,
            "action_url": f"/admin/network/radius/servers/{server_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/server_form.html", context)

    try:
        assert payload is not None
        updated_server = radius_service.radius_servers.update(
            db=db,
            server_id=server_id,
            payload=payload,
        )
        after_snapshot = model_to_dict(updated_server)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="radius_server",
            entity_id=str(updated_server.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server_data,
        "action_url": f"/admin/network/radius/servers/{server_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get("/radius/clients/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_client_new(request: Request, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": None,
        "servers": servers,
        "action_url": "/admin/network/radius/clients",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get("/radius/clients/{client_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_client_edit(request: Request, client_id: str, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)

    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS client not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client,
        "servers": servers,
        "action_url": f"/admin/network/radius/clients/{client_id}",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post("/radius/clients", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def radius_client_create(request: Request, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)
    values = web_network_radius_service.parse_client_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_client_form(values, require_secret=True)
    client_data = web_network_radius_service.client_form_data(values)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "client": client_data,
            "servers": servers,
            "action_url": "/admin/network/radius/clients",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/client_form.html", context)

    try:
        payload = web_network_radius_service.build_client_create_payload(values)
        client = radius_service.radius_clients.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="radius_client",
            entity_id=str(client.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"client_ip": client.client_ip, "server_id": str(client.server_id)},
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client_data,
        "servers": servers,
        "action_url": "/admin/network/radius/clients",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post("/radius/clients/{client_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def radius_client_update(request: Request, client_id: str, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)

    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS client not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    before_snapshot = model_to_dict(client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS)
    values = web_network_radius_service.parse_client_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_client_form(values, require_secret=False)
    client_data = web_network_radius_service.client_form_data(
        values,
        client_id=str(client.id),
    )

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "client": client_data,
            "servers": servers,
            "action_url": f"/admin/network/radius/clients/{client_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/client_form.html", context)

    try:
        payload = web_network_radius_service.build_client_update_payload(values)
        updated_client = radius_service.radius_clients.update(
            db=db,
            client_id=client_id,
            payload=payload,
        )
        after_snapshot = model_to_dict(updated_client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="radius_client",
            entity_id=str(updated_client.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client_data,
        "servers": servers,
        "action_url": f"/admin/network/radius/clients/{client_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get("/radius/profiles/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_profile_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update(web_network_radius_service.profile_new_form_data())
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.get("/radius/profiles/{profile_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def radius_profile_edit(request: Request, profile_id: str, db: Session = Depends(get_db)):
    form_data = web_network_radius_service.profile_edit_form_data(db, profile_id)
    if not form_data:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS profile not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update(form_data)
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.post("/radius/profiles", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def radius_profile_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    profile_data, attributes, error = web_network_radius_service.parse_profile_form(form)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "profile": profile_data,
            "attributes": attributes,
            "vendors": web_network_radius_service.profile_vendors(),
            "action_url": "/admin/network/radius/profiles",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/profile_form.html", context)

    profile, metadata = web_network_radius_service.create_profile(db, profile_data, attributes)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="radius_profile",
        entity_id=str(profile.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.post("/radius/profiles/{profile_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def radius_profile_update(request: Request, profile_id: str, db: Session = Depends(get_db)):
    profile_form_data = web_network_radius_service.profile_edit_form_data(db, profile_id)
    if not profile_form_data:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS profile not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    form = parse_form_data_sync(request)
    profile_data, attributes, error = web_network_radius_service.parse_profile_form(form)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "profile": profile_data,
            "attributes": attributes,
            "vendors": web_network_radius_service.profile_vendors(),
            "action_url": f"/admin/network/radius/profiles/{profile_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/profile_form.html", context)

    updated_profile, metadata = web_network_radius_service.update_profile(
        db=db,
        profile_id=profile_id,
        profile_data=profile_data,
        attributes=attributes,
    )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="radius_profile",
        entity_id=str(updated_profile.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse("/admin/network/radius", status_code=303)


