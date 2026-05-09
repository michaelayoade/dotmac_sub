"""Admin routes for network device groups."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services.auth_dependencies import require_permission
from app.services.network import device_groups as device_group_service
from app.web.templates import templates

router = APIRouter(prefix="/network/device-groups", tags=["web-admin-network-device-groups"])


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
    dependencies=[Depends(require_permission("network:read"))],
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
    dependencies=[Depends(require_permission("network:write"))],
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
    dependencies=[Depends(require_permission("network:read"))],
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
    return templates.TemplateResponse("admin/network/device-groups/detail.html", context)


@router.post(
    "/{group_id}/members",
    dependencies=[Depends(require_permission("network:write"))],
)
def device_group_add_member(
    request: Request,
    group_id: str,
    device_type: str = Form("ont"),
    device_id: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        device_group_service.add_device_group_member(
            db,
            group_id=group_id,
            device_type=device_type,
            device_id=device_id,
            added_by=web_admin_service.get_actor_id(request),
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
    "/{group_id}/members/{member_id}/remove",
    dependencies=[Depends(require_permission("network:write"))],
)
def device_group_remove_member(
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
    dependencies=[Depends(require_permission("network:write"))],
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
        db.commit()
        message = (
            f"Queued {result['action']} for {result['ont_count']} ONT(s)"
        )
        status = "success"
    except Exception as exc:
        db.rollback()
        message = str(exc)
        status = "error"
    return RedirectResponse(
        f"/admin/network/device-groups/{group_id}?status={status}&message={quote_plus(message)}",
        status_code=303,
    )
