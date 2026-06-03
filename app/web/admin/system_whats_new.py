"""Admin What's New management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import admin_whats_new as admin_whats_new_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/system/whats-new", tags=["web-admin-whats-new"])


def _base_context(
    request: Request,
    db: Session,
    *,
    active_page: str = "settings-hub",
) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def whats_new_index(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db)
    context.update(
        {
            "items": admin_whats_new_service.list_items(db, status=status),
            "stats": admin_whats_new_service.get_stats(db),
            "status_filter": status or "",
            "statuses": admin_whats_new_service.ALL_STATUSES,
        }
    )
    return templates.TemplateResponse("admin/system/whats_new/index.html", context)


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def whats_new_new(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db)
    context.update(
        {
            "item": None,
            "action_url": "/admin/system/whats-new/new",
            "statuses": admin_whats_new_service.ALL_STATUSES,
            "form_values": {},
        }
    )
    return templates.TemplateResponse("admin/system/whats_new/form.html", context)


@router.post(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def whats_new_create(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = parse_form_data_sync(request)
    try:
        values = admin_whats_new_service.parse_form_values(form)
        error = admin_whats_new_service.validate_values(values)
    except ValueError as exc:
        values = {
            "title": str(form.get("title") or "").strip(),
            "message": str(form.get("message") or "").strip(),
            "benefit_one": str(form.get("benefit_one") or "").strip(),
            "benefit_two": str(form.get("benefit_two") or "").strip(),
            "benefit_three": str(form.get("benefit_three") or "").strip(),
            "button_text": str(form.get("button_text") or "").strip(),
            "button_link": str(form.get("button_link") or "").strip(),
            "status": str(form.get("status") or "draft").strip().lower(),
            "starts_at": str(form.get("starts_at") or "").strip(),
            "ends_at": str(form.get("ends_at") or "").strip(),
        }
        error = str(exc)
    if error:
        context = _base_context(request, db)
        context.update(
            {
                "item": None,
                "action_url": "/admin/system/whats-new/new",
                "statuses": admin_whats_new_service.ALL_STATUSES,
                "form_values": values,
                "error": error,
            }
        )
        return templates.TemplateResponse(
            "admin/system/whats_new/form.html",
            context,
            status_code=400,
        )

    admin_whats_new_service.create_item(db, values)
    return RedirectResponse("/admin/system/whats-new", status_code=303)


@router.get(
    "/{item_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def whats_new_edit(
    request: Request,
    item_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    item = admin_whats_new_service.get_item(db, item_id)
    if not item:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "What's New item not found"},
            status_code=404,
        )
    context = _base_context(request, db)
    context.update(
        {
            "item": item,
            "action_url": f"/admin/system/whats-new/{item_id}/edit",
            "statuses": admin_whats_new_service.ALL_STATUSES,
            "form_values": {},
        }
    )
    return templates.TemplateResponse("admin/system/whats_new/form.html", context)


@router.post(
    "/{item_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def whats_new_update(
    request: Request,
    item_id: str,
    db: Session = Depends(get_db),
) -> Response:
    item = admin_whats_new_service.get_item(db, item_id)
    if not item:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "What's New item not found"},
            status_code=404,
        )
    form = parse_form_data_sync(request)
    try:
        values = admin_whats_new_service.parse_form_values(form)
        error = admin_whats_new_service.validate_values(values)
    except ValueError as exc:
        values = {
            "title": str(form.get("title") or "").strip(),
            "message": str(form.get("message") or "").strip(),
            "benefit_one": str(form.get("benefit_one") or "").strip(),
            "benefit_two": str(form.get("benefit_two") or "").strip(),
            "benefit_three": str(form.get("benefit_three") or "").strip(),
            "button_text": str(form.get("button_text") or "").strip(),
            "button_link": str(form.get("button_link") or "").strip(),
            "status": str(form.get("status") or "draft").strip().lower(),
            "starts_at": str(form.get("starts_at") or "").strip(),
            "ends_at": str(form.get("ends_at") or "").strip(),
        }
        error = str(exc)
    if error:
        context = _base_context(request, db)
        context.update(
            {
                "item": item,
                "action_url": f"/admin/system/whats-new/{item_id}/edit",
                "statuses": admin_whats_new_service.ALL_STATUSES,
                "form_values": values,
                "error": error,
            }
        )
        return templates.TemplateResponse(
            "admin/system/whats_new/form.html",
            context,
            status_code=400,
        )

    admin_whats_new_service.update_item(db, item, values)
    return RedirectResponse("/admin/system/whats-new", status_code=303)


@router.post(
    "/{item_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def whats_new_update_status(
    request: Request,
    item_id: str,
    db: Session = Depends(get_db),
) -> Response:
    item = admin_whats_new_service.get_item(db, item_id)
    if not item:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "What's New item not found"},
            status_code=404,
        )
    form = parse_form_data_sync(request)
    status = str(form.get("status") or "").strip().lower()
    try:
        admin_whats_new_service.set_status(db, item, status)
    except ValueError:
        return RedirectResponse(
            "/admin/system/whats-new?status=invalid", status_code=303
        )
    return RedirectResponse("/admin/system/whats-new", status_code=303)
