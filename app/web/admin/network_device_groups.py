"""Admin routes for network device groups."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission
from app.services.network import device_groups as device_group_service
from app.web.templates import templates

router = APIRouter(
    prefix="/network/device-groups", tags=["web-admin-network-device-groups"]
)


def _base_context(request: Request, db: Session) -> dict:
    return {
        "request": request,
        "active_page": "device-groups",
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def device_groups_index(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db)
    rows = device_group_service.list_device_groups(db)
    context.update(
        {
            "rows": rows,
            "message": request.query_params.get("message", ""),
            "result_status": request.query_params.get("status", ""),
            "total_groups": len(rows),
            "total_members": sum(int(row["member_count"]) for row in rows),
        }
    )
    return templates.TemplateResponse("admin/network/device-groups/index.html", context)


@router.post(
    "",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_groups_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        group = device_group_service.create_device_group(
            db,
            name=name,
            description=description,
            created_by=web_admin_service.get_actor_id(request),
        )
        log_audit_event(
            db,
            request,
            action="device_group_created",
            entity_type="device_group",
            entity_id=str(group.id),
            actor_id=web_admin_service.get_actor_id(request),
            metadata={"name": group.name},
        )
        db.commit()
        return RedirectResponse(
            f"/admin/network/device-groups/{group.id}?status=success&message={quote_plus('Device group created')}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/network/device-groups?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )


@router.get(
    "/{group_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def device_group_detail(
    request: Request,
    group_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db)
    try:
        context.update(device_group_service.device_group_detail_context(db, group_id))
    except device_group_service.DeviceGroupError as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc)},
            status_code=404,
        )
    context.update(
        {
            "message": request.query_params.get("message", ""),
            "result_status": request.query_params.get("status", ""),
        }
    )
    return templates.TemplateResponse(
        "admin/network/device-groups/detail.html", context
    )


@router.get(
    "/{group_id}/member-candidates",
    dependencies=[Depends(require_permission("network:device:read"))],
)
def device_group_member_candidates(
    group_id: str,
    device_type: str = Query("ont"),
    q: str = Query(""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    try:
        items = device_group_service.list_device_group_member_candidates(
            db,
            group_id=group_id,
            device_type=device_type,
            search=q,
            limit=50,
        )
        return JSONResponse({"items": items})
    except device_group_service.DeviceGroupError as exc:
        return JSONResponse({"items": [], "error": str(exc)}, status_code=400)


@router.post(
    "/{group_id}/settings",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_group_update(
    request: Request,
    group_id: str,
    name: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        group = device_group_service.update_device_group(
            db,
            group_id=group_id,
            name=name,
            description=description,
        )
        log_audit_event(
            db,
            request,
            action="device_group_updated",
            entity_type="device_group",
            entity_id=str(group.id),
            actor_id=web_admin_service.get_actor_id(request),
            metadata={"name": group.name},
        )
        db.commit()
        status = "success"
        message = "Device group updated"
    except Exception as exc:
        db.rollback()
        status = "error"
        message = str(exc)
    return RedirectResponse(
        f"/admin/network/device-groups/{group_id}?status={status}&message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/{group_id}/archive",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_group_archive(
    request: Request,
    group_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        group = device_group_service.archive_device_group(db, group_id=group_id)
        log_audit_event(
            db,
            request,
            action="device_group_archived",
            entity_type="device_group",
            entity_id=str(group.id),
            actor_id=web_admin_service.get_actor_id(request),
            metadata={"name": group.name},
        )
        db.commit()
        return RedirectResponse(
            "/admin/network/device-groups?status=success&message=Device+group+archived",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/network/device-groups/{group_id}?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )


@router.post(
    "/{group_id}/members",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_group_add_member(
    request: Request,
    group_id: str,
    device_type: str = Form("ont"),
    device_id: str = Form(""),
    member_selector: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        selected_type, selected_id = _parse_member_selector(
            member_selector,
            fallback_type=device_type,
            fallback_id=device_id,
        )
        device_group_service.add_device_group_member(
            db,
            group_id=group_id,
            device_type=selected_type,
            device_id=selected_id,
            added_by=web_admin_service.get_actor_id(request),
        )
        log_audit_event(
            db,
            request,
            action="device_group_member_added",
            entity_type="device_group",
            entity_id=group_id,
            actor_id=web_admin_service.get_actor_id(request),
            metadata={"device_type": selected_type, "device_id": selected_id},
        )
        db.commit()
        status = "success"
        message = "Device added to group"
    except Exception as exc:
        db.rollback()
        status = "error"
        message = str(exc)
    return RedirectResponse(
        f"/admin/network/device-groups/{group_id}?status={status}&message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/{group_id}/members/import",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_group_import_members(
    request: Request,
    group_id: str,
    device_type: str = Form("ont"),
    identifiers: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        result = device_group_service.add_device_group_members_from_text(
            db,
            group_id=group_id,
            device_type=device_type,
            identifiers=identifiers,
            added_by=web_admin_service.get_actor_id(request),
        )
        log_audit_event(
            db,
            request,
            action="device_group_members_imported",
            entity_type="device_group",
            entity_id=group_id,
            actor_id=web_admin_service.get_actor_id(request),
            metadata=result,
        )
        db.commit()
        message = (
            f"Imported {result['added']} device(s); "
            f"{result['existing']} already present, {len(result['missing'])} not found"
        )
        status = "success" if not result["missing"] else "error"
    except Exception as exc:
        db.rollback()
        status = "error"
        message = str(exc)
    return RedirectResponse(
        f"/admin/network/device-groups/{group_id}?status={status}&message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/{group_id}/members/import-filter",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_group_import_members_by_filter(
    request: Request,
    group_id: str,
    device_type: str = Form("ont"),
    search: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        result = device_group_service.add_device_group_members_from_filter(
            db,
            group_id=group_id,
            device_type=device_type,
            search=search,
            added_by=web_admin_service.get_actor_id(request),
        )
        log_audit_event(
            db,
            request,
            action="device_group_members_imported_by_filter",
            entity_type="device_group",
            entity_id=group_id,
            actor_id=web_admin_service.get_actor_id(request),
            metadata=result,
        )
        db.commit()
        status = "success"
        message = f"Imported {result['added']} matching device(s)"
    except Exception as exc:
        db.rollback()
        status = "error"
        message = str(exc)
    return RedirectResponse(
        f"/admin/network/device-groups/{group_id}?status={status}&message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/{group_id}/members/{member_id}/remove",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_group_remove_member(
    request: Request,
    group_id: str,
    member_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        device_group_service.remove_device_group_member(
            db,
            group_id=group_id,
            member_id=member_id,
        )
        log_audit_event(
            db,
            request,
            action="device_group_member_removed",
            entity_type="device_group",
            entity_id=group_id,
            actor_id=web_admin_service.get_actor_id(request),
            metadata={"member_id": member_id},
        )
        db.commit()
        status = "success"
        message = "Device removed from group"
    except Exception as exc:
        db.rollback()
        status = "error"
        message = str(exc)
    return RedirectResponse(
        f"/admin/network/device-groups/{group_id}?status={status}&message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/{group_id}/actions",
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_group_action(
    request: Request,
    group_id: str,
    action: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        result = device_group_service.enqueue_ont_group_action(
            db,
            group_id=group_id,
            action=action,
            initiated_by=web_admin_service.get_actor_id(request),
        )
        log_audit_event(
            db,
            request,
            action="device_group_action_queued",
            entity_type="device_group",
            entity_id=group_id,
            actor_id=web_admin_service.get_actor_id(request),
            metadata=result,
        )
        db.commit()
        message = f"Queued {result['action']} for {result['ont_count']} ONT(s)"
        status = "success"
    except Exception as exc:
        db.rollback()
        message = str(exc)
        status = "error"
    return RedirectResponse(
        f"/admin/network/device-groups/{group_id}?status={status}&message={quote_plus(message)}",
        status_code=303,
    )


def _parse_member_selector(
    value: str | None,
    *,
    fallback_type: str,
    fallback_id: str,
) -> tuple[str, str]:
    text = str(value or "").strip()
    if ":" in text:
        device_type, _, device_id = text.partition(":")
        return device_type.strip(), device_id.strip()
    return str(fallback_type or "").strip(), str(fallback_id or "").strip()
