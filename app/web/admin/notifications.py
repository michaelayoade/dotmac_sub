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
from app.schemas.notification import (
    AlertNotificationPolicyCreate,
    AlertNotificationPolicyStepCreate,
    AlertNotificationPolicyUpdate,
    OnCallRotationCreate,
    OnCallRotationMemberCreate,
    OnCallRotationUpdate,
)
from app.services import notification as notification_service
from app.services import notification_template_renderer as template_renderer
from app.services import web_admin_notifications as web_admin_notifications_service
from app.services import (
    web_notifications_alert_policies as web_alert_policies_service,
)
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
    test_variables_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Send a test notification using this template."""
    from app.services import email as email_service
    from app.services.integrations.connectors import whatsapp as whatsapp_service
    from app.services import sms as sms_service

    try:
        template = notification_service.templates.get(db=db, template_id=str(template_id))
        variables = template_renderer.default_preview_variables()
        if test_variables_json and test_variables_json.strip():
            parsed = json.loads(test_variables_json)
            if not isinstance(parsed, dict):
                raise ValueError("test_variables_json must be a JSON object")
            for key, value in parsed.items():
                variables[str(key)] = "" if value is None else str(value)
        rendered_subject = template_renderer.render_template_text(
            template.subject or "Test Notification",
            variables,
        )
        rendered_body = template_renderer.render_template_text(template.body, variables)

        # Send test notification based on channel
        if template.channel == NotificationChannel.sms:
            sms_service.send_sms(
                db=db,
                to_phone=test_recipient.strip(),
                body=rendered_body,
                track=True,
            )
            message = f"Test SMS sent to {test_recipient}"
        elif template.channel == NotificationChannel.email:
            email_service.send_email(
                db=db,
                to_email=test_recipient.strip(),
                subject=rendered_subject,
                body_html=rendered_body,
                activity="notification_test",
            )
            message = f"Test email sent to {test_recipient}"
        elif template.channel == NotificationChannel.whatsapp:
            result = whatsapp_service.send_text_message(
                db=db,
                recipient=test_recipient.strip(),
                body=rendered_body,
                dry_run=False,
            )
            if not result.get("ok"):
                raise RuntimeError(str(result.get("response") or "Failed to send WhatsApp test"))
            message = f"Test WhatsApp message sent to {test_recipient}"
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


@router.post(
    "/templates/{template_id}/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def notification_template_preview(
    request: Request,
    template_id: UUID,
    test_variables_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Render template preview with variable substitution."""
    template = notification_service.templates.get(db=db, template_id=str(template_id))
    variables = template_renderer.default_preview_variables()
    if test_variables_json and test_variables_json.strip():
        parsed = json.loads(test_variables_json)
        if not isinstance(parsed, dict):
            raise ValueError("test_variables_json must be a JSON object")
        for key, value in parsed.items():
            variables[str(key)] = "" if value is None else str(value)

    rendered_subject = template_renderer.render_template_text(
        template.subject or "",
        variables,
    )
    rendered_body = template_renderer.render_template_text(template.body, variables)
    return templates.TemplateResponse(
        "admin/notifications/_template_preview.html",
        {
            "request": request,
            "rendered_subject": rendered_subject,
            "rendered_body": rendered_body,
            "variables": variables,
            "channel": template.channel.value,
        },
    )


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


# ---------------------------------------------------------------------------
# Alert Notification Policies
# ---------------------------------------------------------------------------


@router.get(
    "/alert-policies",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:read"))],
)
def alert_policies_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List alert notification policies."""
    state = web_alert_policies_service.alert_policies_list_data(
        db, page=page, per_page=per_page
    )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/alert_policies.html",
        {
            "request": request,
            **state,
            "active_page": "alert-policies",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/alert-policies/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def alert_policy_new(request: Request, db: Session = Depends(get_db)):
    """Create new alert policy form."""
    state = web_alert_policies_service.alert_policy_form_data(db)
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/alert_policy_form.html",
        {
            "request": request,
            **state,
            "action_url": "/admin/notifications/alert-policies",
            "form_title": "New Alert Policy",
            "submit_label": "Create Policy",
            "active_page": "alert-policies",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/alert-policies",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def alert_policy_create(
    request: Request,
    name: str = Form(...),
    channel: str = Form(...),
    recipient: str = Form(...),
    severity_min: str = Form("warning"),
    template_id: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create an alert notification policy."""
    from app.models.network_monitoring import AlertSeverity

    try:
        payload = AlertNotificationPolicyCreate(
            name=name.strip(),
            channel=NotificationChannel(channel),
            recipient=recipient.strip(),
            severity_min=AlertSeverity(severity_min),
            template_id=UUID(template_id) if template_id else None,
            notes=notes.strip() if notes else None,
            is_active=is_active is not None,
        )
        policy = notification_service.alert_notification_policies.create(
            db=db, payload=payload
        )
        return RedirectResponse(
            url=f"/admin/notifications/alert-policies/{policy.id}",
            status_code=303,
        )
    except Exception as exc:
        state = web_alert_policies_service.alert_policy_form_data(db)
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/notifications/alert_policy_form.html",
            {
                "request": request,
                **state,
                "action_url": "/admin/notifications/alert-policies",
                "form_title": "New Alert Policy",
                "submit_label": "Create Policy",
                "error": str(exc),
                "active_page": "alert-policies",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/alert-policies/{policy_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:read"))],
)
def alert_policy_detail(
    request: Request,
    policy_id: UUID,
    db: Session = Depends(get_db),
):
    """View and edit alert policy with escalation steps."""
    state = web_alert_policies_service.alert_policy_detail_data(
        db, policy_id=str(policy_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Alert policy not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/alert_policy_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/notifications/alert-policies/{policy_id}",
            "form_title": "Edit Alert Policy",
            "submit_label": "Update Policy",
            "active_page": "alert-policies",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/alert-policies/{policy_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def alert_policy_update(
    request: Request,
    policy_id: UUID,
    name: str = Form(...),
    channel: str = Form(...),
    recipient: str = Form(...),
    severity_min: str = Form("warning"),
    template_id: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update an alert notification policy."""
    from app.models.network_monitoring import AlertSeverity

    try:
        payload = AlertNotificationPolicyUpdate(
            name=name.strip(),
            channel=NotificationChannel(channel),
            recipient=recipient.strip(),
            severity_min=AlertSeverity(severity_min),
            template_id=UUID(template_id) if template_id else None,
            notes=notes.strip() if notes else None,
            is_active=is_active is not None,
        )
        notification_service.alert_notification_policies.update(
            db=db, policy_id=str(policy_id), payload=payload
        )
        return RedirectResponse(
            url=f"/admin/notifications/alert-policies/{policy_id}",
            status_code=303,
        )
    except Exception as exc:
        state = web_alert_policies_service.alert_policy_detail_data(
            db, policy_id=str(policy_id)
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/notifications/alert_policy_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/notifications/alert-policies/{policy_id}",
                "form_title": "Edit Alert Policy",
                "submit_label": "Update Policy",
                "error": str(exc),
                "active_page": "alert-policies",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/alert-policies/{policy_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def alert_policy_delete(policy_id: UUID, db: Session = Depends(get_db)):
    """Delete an alert notification policy."""
    notification_service.alert_notification_policies.delete(
        db=db, policy_id=str(policy_id)
    )
    return RedirectResponse(url="/admin/notifications/alert-policies", status_code=303)


@router.post(
    "/alert-policies/{policy_id}/steps",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def alert_policy_step_create(
    request: Request,
    policy_id: UUID,
    step_index: int = Form(0),
    delay_minutes: int = Form(0),
    step_channel: str = Form("email"),
    step_recipient: str | None = Form(None),
    step_rotation_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Add an escalation step to an alert policy."""
    try:
        payload = AlertNotificationPolicyStepCreate(
            policy_id=policy_id,
            step_index=step_index,
            delay_minutes=delay_minutes,
            channel=NotificationChannel(step_channel),
            recipient=step_recipient.strip() if step_recipient else None,
            rotation_id=UUID(step_rotation_id) if step_rotation_id else None,
        )
        notification_service.alert_notification_policy_steps.create(
            db=db, payload=payload
        )
    except Exception:
        pass  # Redirect back regardless
    return RedirectResponse(
        url=f"/admin/notifications/alert-policies/{policy_id}",
        status_code=303,
    )


@router.post(
    "/alert-policies/{policy_id}/steps/{step_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def alert_policy_step_delete(
    policy_id: UUID,
    step_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete an escalation step."""
    notification_service.alert_notification_policy_steps.delete(
        db=db, step_id=str(step_id)
    )
    return RedirectResponse(
        url=f"/admin/notifications/alert-policies/{policy_id}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# On-Call Rotations
# ---------------------------------------------------------------------------


@router.get(
    "/oncall-rotations",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:read"))],
)
def oncall_rotations_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List on-call rotations."""
    state = web_alert_policies_service.oncall_rotations_list_data(
        db, page=page, per_page=per_page
    )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/oncall_rotations.html",
        {
            "request": request,
            **state,
            "active_page": "oncall-rotations",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/oncall-rotations",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def oncall_rotation_create(
    request: Request,
    name: str = Form(...),
    timezone: str = Form("UTC"),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create an on-call rotation."""
    try:
        payload = OnCallRotationCreate(
            name=name.strip(),
            timezone=timezone.strip(),
            notes=notes.strip() if notes else None,
        )
        rotation = notification_service.on_call_rotations.create(
            db=db, payload=payload
        )
        return RedirectResponse(
            url=f"/admin/notifications/oncall-rotations/{rotation.id}",
            status_code=303,
        )
    except Exception as exc:
        state = web_alert_policies_service.oncall_rotations_list_data(
            db, page=1, per_page=25
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/notifications/oncall_rotations.html",
            {
                "request": request,
                **state,
                "error": str(exc),
                "active_page": "oncall-rotations",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/oncall-rotations/{rotation_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:read"))],
)
def oncall_rotation_detail(
    request: Request,
    rotation_id: UUID,
    db: Session = Depends(get_db),
):
    """View on-call rotation with members."""
    state = web_alert_policies_service.oncall_rotation_detail_data(
        db, rotation_id=str(rotation_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "On-call rotation not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/notifications/oncall_form.html",
        {
            "request": request,
            **state,
            "active_page": "oncall-rotations",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/oncall-rotations/{rotation_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def oncall_rotation_update(
    request: Request,
    rotation_id: UUID,
    name: str = Form(...),
    timezone: str = Form("UTC"),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update an on-call rotation."""
    try:
        payload = OnCallRotationUpdate(
            name=name.strip(),
            timezone=timezone.strip(),
            notes=notes.strip() if notes else None,
            is_active=is_active is not None,
        )
        notification_service.on_call_rotations.update(
            db=db, rotation_id=str(rotation_id), payload=payload
        )
        return RedirectResponse(
            url=f"/admin/notifications/oncall-rotations/{rotation_id}",
            status_code=303,
        )
    except Exception as exc:
        state = web_alert_policies_service.oncall_rotation_detail_data(
            db, rotation_id=str(rotation_id)
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/notifications/oncall_form.html",
            {
                "request": request,
                **(state or {}),
                "error": str(exc),
                "active_page": "oncall-rotations",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/oncall-rotations/{rotation_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def oncall_rotation_delete(rotation_id: UUID, db: Session = Depends(get_db)):
    """Delete an on-call rotation."""
    notification_service.on_call_rotations.delete(
        db=db, rotation_id=str(rotation_id)
    )
    return RedirectResponse(
        url="/admin/notifications/oncall-rotations", status_code=303
    )


@router.post(
    "/oncall-rotations/{rotation_id}/members",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def oncall_rotation_member_create(
    request: Request,
    rotation_id: UUID,
    member_name: str = Form(...),
    member_contact: str = Form(...),
    member_priority: int = Form(0),
    db: Session = Depends(get_db),
):
    """Add a member to an on-call rotation."""
    try:
        payload = OnCallRotationMemberCreate(
            rotation_id=rotation_id,
            name=member_name.strip(),
            contact=member_contact.strip(),
            priority=member_priority,
        )
        notification_service.on_call_rotation_members.create(
            db=db, payload=payload
        )
    except Exception:
        pass  # Redirect back regardless
    return RedirectResponse(
        url=f"/admin/notifications/oncall-rotations/{rotation_id}",
        status_code=303,
    )


@router.post(
    "/oncall-rotations/{rotation_id}/members/{member_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def oncall_rotation_member_delete(
    rotation_id: UUID,
    member_id: UUID,
    db: Session = Depends(get_db),
):
    """Remove a member from an on-call rotation."""
    notification_service.on_call_rotation_members.delete(
        db=db, member_id=str(member_id)
    )
    return RedirectResponse(
        url=f"/admin/notifications/oncall-rotations/{rotation_id}",
        status_code=303,
    )
