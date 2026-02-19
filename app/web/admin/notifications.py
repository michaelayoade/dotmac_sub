"""Admin notifications management routes."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.notification import (
    DeliveryStatus,
    NotificationChannel,
    NotificationStatus,
)
from app.services import notification as notification_service
from app.services import web_admin_notifications as web_admin_notifications_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/notifications", tags=["web-admin-notifications"])


def _htmx_error_response(
    message: str,
    status_code: int = 409,
    title: str = "Error",
    reswap: str | None = None,
) -> Response:
    trigger = {
        "showToast": {
            "type": "error",
            "title": title,
            "message": message,
        }
    }
    headers = {"HX-Trigger": json.dumps(trigger)}
    if reswap:
        headers["HX-Reswap"] = reswap
    return Response(status_code=status_code, headers=headers)


@router.get("", response_class=HTMLResponse)
def notifications_menu(request: Request, db: Session = Depends(get_db)):
    """Notifications dropdown menu."""
    return web_admin_notifications_service.notifications_menu(request, db)


@router.get("/templates", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:read"))])
def notification_templates_list(
    request: Request,
    channel: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List notification templates."""
    offset = (page - 1) * per_page

    template_list = notification_service.templates.list(
        db=db,
        channel=channel if channel else None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=per_page,
        offset=offset,
    )

    total = notification_service.templates.count(db=db, channel=channel if channel else None)
    total_pages = (total + per_page - 1) // per_page if total else 1

    channel_counts = notification_service.templates.channel_counts(db)

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/templates_list.html",
        {
            "request": request,
            "templates": template_list,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "channel": channel,
            "channel_counts": channel_counts,
            "channels": [c.value for c in NotificationChannel],
            "active_page": "notification-templates",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/templates/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:write"))])
def notification_template_new(request: Request, db: Session = Depends(get_db)):
    """Create new notification template form."""
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/template_form.html",
        {
            "request": request,
            "channels": [c.value for c in NotificationChannel],
            "action_url": "/admin/notifications/templates",
            "form_title": "New Notification Template",
            "submit_label": "Create Template",
            "active_page": "notification-templates",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/templates", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:write"))])
def notification_template_create(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    channel: str = Form(...),
    subject: str | None = Form(None),
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create a notification template."""
    from app.schemas.notification import NotificationTemplateCreate

    try:
        payload = NotificationTemplateCreate(
            name=name.strip(),
            code=code.strip().lower().replace(" ", "_"),
            channel=NotificationChannel(channel),
            subject=subject.strip() if subject else None,
            body=body.strip(),
        )
        template = notification_service.templates.create(db=db, payload=payload)
        return RedirectResponse(url=f"/admin/notifications/templates/{template.id}", status_code=303)
    except Exception as exc:
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/notifications/template_form.html",
            {
                "request": request,
                "channels": [c.value for c in NotificationChannel],
                "action_url": "/admin/notifications/templates",
                "form_title": "New Notification Template",
                "submit_label": "Create Template",
                "error": str(exc),
                "active_page": "notification-templates",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get("/templates/{template_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:read"))])
def notification_template_detail(
    request: Request,
    template_id: UUID,
    db: Session = Depends(get_db),
):
    """View and edit notification template."""
    template = notification_service.templates.get(db=db, template_id=str(template_id))
    if not template:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Template not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/template_form.html",
        {
            "request": request,
            "template": template,
            "channels": [c.value for c in NotificationChannel],
            "action_url": f"/admin/notifications/templates/{template_id}",
            "form_title": "Edit Notification Template",
            "submit_label": "Update Template",
            "active_page": "notification-templates",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/templates/{template_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:write"))])
def notification_template_update(
    request: Request,
    template_id: UUID,
    name: str = Form(...),
    code: str = Form(...),
    channel: str = Form(...),
    subject: str | None = Form(None),
    body: str = Form(...),
    is_active: bool = Form(True),
    db: Session = Depends(get_db),
):
    """Update a notification template."""
    from app.schemas.notification import NotificationTemplateUpdate

    try:
        payload = NotificationTemplateUpdate(
            name=name.strip(),
            code=code.strip().lower().replace(" ", "_"),
            channel=NotificationChannel(channel),
            subject=subject.strip() if subject else None,
            body=body.strip(),
            is_active=is_active,
        )
        notification_service.templates.update(db=db, template_id=str(template_id), payload=payload)
        return RedirectResponse(url=f"/admin/notifications/templates/{template_id}", status_code=303)
    except Exception as exc:
        template = notification_service.templates.get(db=db, template_id=str(template_id))
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/notifications/template_form.html",
            {
                "request": request,
                "template": template,
                "channels": [c.value for c in NotificationChannel],
                "action_url": f"/admin/notifications/templates/{template_id}",
                "form_title": "Edit Notification Template",
                "submit_label": "Update Template",
                "error": str(exc),
                "active_page": "notification-templates",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post("/templates/{template_id}/test", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:write"))])
def notification_template_test(
    request: Request,
    template_id: UUID,
    test_recipient: str = Form(...),
    db: Session = Depends(get_db),
):
    """Send a test notification using this template."""
    from app.services import email as email_service
    from app.services import sms as sms_service

    try:
        template = notification_service.templates.get(db=db, template_id=str(template_id))

        # Send test notification based on channel
        if template.channel == NotificationChannel.sms:
            sms_service.send_sms(
                db=db,
                to_phone=test_recipient.strip(),
                body=template.body,
                track=True,
            )
            message = f"Test SMS sent to {test_recipient}"
        elif template.channel == NotificationChannel.email:
            email_service.send_email(
                db=db,
                to_email=test_recipient.strip(),
                subject=template.subject or "Test Notification",
                body_html=template.body,
            )
            message = f"Test email sent to {test_recipient}"
        else:
            message = f"Test notification queued for {template.channel.value}"

        if request.headers.get("HX-Request"):
            trigger = {
                "showToast": {
                    "type": "success",
                    "title": "Test Sent",
                    "message": message,
                }
            }
            return Response(status_code=200, headers={"HX-Trigger": json.dumps(trigger)})
        return RedirectResponse(url=f"/admin/notifications/templates/{template_id}", status_code=303)
    except Exception as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc), status_code=200, reswap="none")
        return RedirectResponse(url=f"/admin/notifications/templates/{template_id}", status_code=303)


@router.delete("/templates/{template_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:write"))])
@router.post("/templates/{template_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:write"))])
def notification_template_delete(
    request: Request,
    template_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete (deactivate) a notification template."""
    try:
        notification_service.templates.delete(db=db, template_id=str(template_id))
        if request.headers.get("HX-Request"):
            return Response(status_code=200, headers={"HX-Redirect": "/admin/notifications/templates"})
        return RedirectResponse(url="/admin/notifications/templates", status_code=303)
    except Exception as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc), status_code=200, reswap="none")
        return RedirectResponse(url=f"/admin/notifications/templates/{template_id}", status_code=303)


@router.get("/queue", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:read"))])
def notification_queue(
    request: Request,
    status: str | None = None,
    channel: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View pending and queued notifications."""
    offset = (page - 1) * per_page

    notifications_list = notification_service.notifications.list(
        db=db,
        channel=channel if channel else None,
        status=status if status else "queued",
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    effective_status = status if status else "queued"
    total = notification_service.notifications.count(
        db=db,
        channel=channel if channel else None,
        status=effective_status,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1

    status_counts = notification_service.notifications.status_counts(db)

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/queue.html",
        {
            "request": request,
            "notifications": notifications_list,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status": status or "queued",
            "channel": channel,
            "status_counts": status_counts,
            "channels": [c.value for c in NotificationChannel],
            "statuses": [s.value for s in NotificationStatus],
            "active_page": "notification-queue",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/history", response_class=HTMLResponse, dependencies=[Depends(require_permission("system:read"))])
def notification_history(
    request: Request,
    status: str | None = None,
    channel: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View notification delivery history."""
    offset = (page - 1) * per_page

    deliveries = notification_service.deliveries.list(
        db=db,
        notification_id=None,
        status=status if status else None,
        is_active=True,
        order_by="occurred_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    total = notification_service.deliveries.count(
        db=db,
        status=status if status else None,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/history.html",
        {
            "request": request,
            "deliveries": deliveries,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status": status,
            "channel": channel,
            "statuses": [s.value for s in DeliveryStatus],
            "active_page": "notification-history",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )
