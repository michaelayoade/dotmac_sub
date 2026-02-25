"""Admin provisioning management web routes."""

import json
import logging
import re
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.notification import NotificationChannel, NotificationStatus
from app.models.provisioning import (
    AppointmentStatus,
    ProvisioningStepType,
    ProvisioningVendor,
    ServiceOrderStatus,
    ServiceOrderType,
    TaskStatus,
)
from app.schemas.notification import NotificationCreate
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    InstallAppointmentUpdate,
    ProvisioningStepCreate,
    ProvisioningStepUpdate,
    ProvisioningTaskCreate,
    ProvisioningTaskUpdate,
    ProvisioningWorkflowCreate,
    ProvisioningWorkflowUpdate,
    ServiceOrderCreate,
    ServiceOrderUpdate,
)
from app.services import notification as notification_service
from app.services import provisioning as provisioning_service
from app.services import subscriber as subscriber_service
from app.services import web_provisioning_bulk_activate as bulk_activate_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.tasks.provisioning import run_bulk_activation_job

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/provisioning", tags=["web-admin-provisioning"])
MENTION_EMAIL_RE = re.compile(r"@([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _ctx(request: Request, db: Session, active_page: str = "provisioning") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _subscriber_label(subscriber: object) -> str:
    if not subscriber:
        return "Subscriber"
    name = " ".join(
        part
        for part in [
            getattr(subscriber, "first_name", ""),
            getattr(subscriber, "last_name", ""),
        ]
        if part
    )
    return name or getattr(subscriber, "display_name", None) or "Subscriber"


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    return str(current_user.get("subscriber_id")) if current_user else None


def _extract_mentioned_emails(comment: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in MENTION_EMAIL_RE.findall(comment):
        email = match.strip().lower()
        if email and email not in seen:
            seen.add(email)
            ordered.append(email)
    return ordered


def _notify_tagged_users(
    db: Session,
    request: Request,
    *,
    order_id: UUID,
    comment: str,
    mentioned_emails: list[str],
) -> int:
    if not mentioned_emails:
        return 0

    actor_subscriber_id = _actor_id(request)
    recipients = subscriber_service.subscribers.list_active_by_emails(
        db, mentioned_emails
    )

    notified = 0
    base_url = str(request.base_url).rstrip("/")
    order_url = f"{base_url}/admin/provisioning/orders/{order_id}"
    short_order_id = str(order_id)[:8]
    subject = f"You were mentioned in Service Order {short_order_id}"
    body = (
        f"You were tagged in a comment on Service Order {short_order_id}.\n\n"
        f"Comment:\n{comment}\n\n"
        f"Open order: {order_url}"
    )

    for subscriber in recipients:
        if actor_subscriber_id and str(subscriber.id) == actor_subscriber_id:
            continue

        notification_service.notifications.create(
            db,
            NotificationCreate(
                channel=NotificationChannel.push,
                recipient=str(subscriber.id),
                subject=subject,
                body=body,
                status=NotificationStatus.delivered,
                sent_at=datetime.now(UTC),
            ),
        )
        notification_service.notifications.create(
            db,
            NotificationCreate(
                channel=NotificationChannel.email,
                recipient=subscriber.email,
                subject=subject,
                body=body,
                status=NotificationStatus.queued,
            ),
        )
        notified += 1
    return notified


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def provisioning_dashboard(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    stats = provisioning_service.service_orders.get_dashboard_stats(db)

    ctx = _ctx(request, db, "provisioning")
    ctx.update(
        {
            "stats": stats,
            "recent_orders": stats["recent_orders"],
            "statuses": [s.value for s in ServiceOrderStatus],
            "subscriber_label": _subscriber_label,
        }
    )
    return templates.TemplateResponse("admin/provisioning/index.html", ctx)


# ---------------------------------------------------------------------------
# Bulk Service Activation
# ---------------------------------------------------------------------------


@router.get(
    "/bulk-activate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def bulk_activate_page(
    request: Request,
    tab: str | None = Query(default="internet"),
    job_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ctx = _ctx(request, db, "provisioning")
    options = bulk_activate_service.page_options(db, tab=tab or "internet")
    active_job = bulk_activate_service.get_job(db, job_id) if job_id else None
    ctx.update(
        {
            **options,
            "active_job_id": job_id,
            "active_job": active_job,
            "preview": None,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse("admin/provisioning/bulk_activate.html", ctx)


@router.post(
    "/bulk-activate/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
async def bulk_activate_preview(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    filters = bulk_activate_service.parse_filters(dict(form))
    mapping = bulk_activate_service.parse_mapping(dict(form))
    preview = bulk_activate_service.build_preview(db, filters=filters, mapping=mapping)
    return templates.TemplateResponse(
        "admin/provisioning/_bulk_activate_preview.html",
        {"request": request, "preview": preview},
    )


@router.post(
    "/bulk-activate/execute",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
async def bulk_activate_execute(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.web.admin import get_current_user
    from urllib.parse import quote_plus

    form = await request.form()
    filters = bulk_activate_service.parse_filters(dict(form))
    mapping = bulk_activate_service.parse_mapping(dict(form))
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id") or "").strip() or None
    try:
        job = bulk_activate_service.create_job(
            db,
            filters=filters,
            mapping=mapping,
            actor_id=actor_id,
        )
        run_bulk_activation_job.delay(job_id=str(job["job_id"]))
        notice = quote_plus("Bulk activation job queued.")
        return RedirectResponse(
            url=f"/admin/provisioning/bulk-activate?tab={quote_plus(filters.tab)}&job_id={job['job_id']}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        error = quote_plus(str(exc))
        return RedirectResponse(
            url=f"/admin/provisioning/bulk-activate?tab={quote_plus(filters.tab)}&error={error}",
            status_code=303,
        )


@router.get(
    "/bulk-activate/jobs/{job_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def bulk_activate_job_status(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = bulk_activate_service.get_job(db, job_id)
    return templates.TemplateResponse(
        "admin/provisioning/_bulk_activate_job_status.html",
        {"request": request, "job": job},
    )


# ---------------------------------------------------------------------------
# Service Orders - List
# ---------------------------------------------------------------------------


@router.get(
    "/orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def orders_list(
    request: Request,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
) -> HTMLResponse:
    offset = (page - 1) * per_page
    orders = provisioning_service.service_orders.list(
        db,
        subscriber_id=None,
        subscription_id=None,
        status=status,
        order_by="created_at",
        order_dir="desc",
        limit=per_page + 1,
        offset=offset,
    )
    has_next = len(orders) > per_page
    orders = orders[:per_page]

    ctx = _ctx(request, db, "provisioning")
    ctx.update(
        {
            "orders": orders,
            "statuses": [s.value for s in ServiceOrderStatus],
            "current_status": status,
            "search": search or "",
            "page": page,
            "per_page": per_page,
            "has_next": has_next,
            "has_prev": page > 1,
            "subscriber_label": _subscriber_label,
        }
    )
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/provisioning/_table.html", ctx
        )
    return templates.TemplateResponse("admin/provisioning/index.html", {**ctx, "show_orders": True})


# ---------------------------------------------------------------------------
# Service Orders - Create
# ---------------------------------------------------------------------------


@router.get(
    "/orders/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def order_create_form(
    request: Request,
    subscriber: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    prefill_subscriber_id = ""
    prefill_subscriber_label = ""
    if subscriber:
        try:
            selected_subscriber = subscriber_service.subscribers.get(db, subscriber)
            prefill_subscriber_id = str(selected_subscriber.id)
            prefill_subscriber_label = _subscriber_label(selected_subscriber)
        except Exception:
            prefill_subscriber_id = ""
            prefill_subscriber_label = ""

    ctx = _ctx(request, db, "provisioning")
    ctx.update(
        {
            "order": None,
            "order_types": ["new_install", "upgrade", "downgrade", "disconnect"],
            "statuses": [s.value for s in ServiceOrderStatus],
            "prefill_subscriber_id": prefill_subscriber_id,
            "prefill_subscriber_label": prefill_subscriber_label,
        }
    )
    return templates.TemplateResponse("admin/provisioning/form.html", ctx)


@router.post(
    "/orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def order_create(
    request: Request,
    db: Session = Depends(get_db),
    subscriber_id: str = Form(...),
    subscription_id: str | None = Form(default=None),
    order_type: str | None = Form(default=None),
    notes: str | None = Form(default=None),
) -> RedirectResponse:
    parsed_order_type = ServiceOrderType(order_type) if order_type else None
    payload = ServiceOrderCreate(
        account_id=UUID(subscriber_id),
        subscription_id=UUID(subscription_id) if subscription_id else None,
        order_type=parsed_order_type,
        notes=notes,
    )
    order = provisioning_service.service_orders.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="service_order",
        entity_id=str(order.id),
        actor_id=_actor_id(request),
        metadata={
            "order_type": order_type or None,
            "subscription_id": subscription_id or None,
            "subscriber_id": subscriber_id,
        },
    )
    return RedirectResponse(
        url=f"/admin/provisioning/orders/{order.id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Service Orders - Detail
# ---------------------------------------------------------------------------


@router.get(
    "/orders/{order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def order_detail(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    order = provisioning_service.service_orders.get(db, str(order_id))
    if not order:
        ctx = _ctx(request, db, "provisioning")
        return templates.TemplateResponse("admin/errors/404.html", ctx, status_code=404)

    transitions = provisioning_service.service_state_transitions.list(
        db,
        service_order_id=str(order_id),
        order_by="changed_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    workflows = provisioning_service.provisioning_workflows.list(
        db,
        vendor=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    ctx = _ctx(request, db, "provisioning")
    ctx.update(
        {
            "order": order,
            "activities": build_audit_activities(
                db, "service_order", str(order_id), limit=10
            ),
            "transitions": transitions,
            "workflows": workflows,
            "appointment_statuses": [s.value for s in AppointmentStatus],
            "task_statuses": [s.value for s in TaskStatus],
            "subscriber_label": _subscriber_label,
        }
    )
    return templates.TemplateResponse("admin/provisioning/detail.html", ctx)


@router.post(
    "/orders/{order_id}/comments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def order_add_comment(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
    comment: str = Form(...),
) -> RedirectResponse:
    order = provisioning_service.service_orders.get(db, str(order_id))
    if not order:
        return RedirectResponse(url="/admin/provisioning/orders", status_code=303)

    cleaned_comment = comment.strip()
    if not cleaned_comment:
        return RedirectResponse(url=f"/admin/provisioning/orders/{order_id}", status_code=303)

    mentions = _extract_mentioned_emails(cleaned_comment)
    notified_count = _notify_tagged_users(
        db,
        request,
        order_id=order_id,
        comment=cleaned_comment,
        mentioned_emails=mentions,
    )

    log_audit_event(
        db=db,
        request=request,
        action="comment",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={
            "comment": cleaned_comment,
            "mentions": mentions,
            "notified_users": notified_count,
        },
    )
    return RedirectResponse(url=f"/admin/provisioning/orders/{order_id}", status_code=303)


# ---------------------------------------------------------------------------
# Service Orders - Edit
# ---------------------------------------------------------------------------


@router.get(
    "/orders/{order_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def order_edit_form(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    order = provisioning_service.service_orders.get(db, str(order_id))
    if not order:
        ctx = _ctx(request, db, "provisioning")
        return templates.TemplateResponse("admin/errors/404.html", ctx, status_code=404)

    ctx = _ctx(request, db, "provisioning")
    ctx.update(
        {
            "order": order,
            "order_types": ["new_install", "upgrade", "downgrade", "disconnect"],
            "statuses": [s.value for s in ServiceOrderStatus],
        }
    )
    return templates.TemplateResponse("admin/provisioning/form.html", ctx)


@router.post(
    "/orders/{order_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def order_edit(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
    subscriber_id: str | None = Form(default=None),
    subscription_id: str | None = Form(default=None),
    order_type: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    status: str | None = Form(default=None),
) -> RedirectResponse:
    before = provisioning_service.service_orders.get(db, str(order_id))
    before_state = model_to_dict(before) if before else None
    update_data: dict[str, object] = {}
    if subscriber_id:
        update_data["subscriber_id"] = UUID(subscriber_id)
    if subscription_id:
        update_data["subscription_id"] = UUID(subscription_id)
    if status:
        update_data["status"] = ServiceOrderStatus(status)
    if order_type is not None:
        update_data["order_type"] = order_type or None
    if notes is not None:
        update_data["notes"] = notes

    payload = ServiceOrderUpdate.model_validate(update_data)
    provisioning_service.service_orders.update(db, str(order_id), payload)
    after = provisioning_service.service_orders.get(db, str(order_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )
    return RedirectResponse(
        url=f"/admin/provisioning/orders/{order_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Service Orders - Status Change
# ---------------------------------------------------------------------------


@router.post(
    "/orders/{order_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def order_status_change(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
    new_status: str = Form(...),
) -> RedirectResponse:
    before = provisioning_service.service_orders.get(db, str(order_id))
    before_state = model_to_dict(before) if before else None
    payload = ServiceOrderUpdate(status=ServiceOrderStatus(new_status))
    provisioning_service.service_orders.update(db, str(order_id), payload)
    after = provisioning_service.service_orders.get(db, str(order_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )
    return RedirectResponse(
        url=f"/admin/provisioning/orders/{order_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Appointments (on order)
# ---------------------------------------------------------------------------


@router.post(
    "/orders/{order_id}/appointments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def add_appointment(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
    scheduled_start: str = Form(...),
    scheduled_end: str = Form(...),
    technician: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_self_install: bool = Form(default=False),
) -> RedirectResponse:
    start = _parse_datetime(scheduled_start)
    end = _parse_datetime(scheduled_end)
    if not start or not end:
        return RedirectResponse(
            url=f"/admin/provisioning/orders/{order_id}", status_code=303
        )
    payload = InstallAppointmentCreate(
        service_order_id=order_id,
        scheduled_start=start,
        scheduled_end=end,
        technician=technician or None,
        notes=notes or None,
        is_self_install=is_self_install,
    )
    appointment = provisioning_service.install_appointments.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={
            "appointment_id": str(appointment.id),
            "technician": technician or None,
            "scheduled_start": start.isoformat(),
            "scheduled_end": end.isoformat(),
        },
    )
    return RedirectResponse(
        url=f"/admin/provisioning/orders/{order_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Tasks (on order)
# ---------------------------------------------------------------------------


@router.post(
    "/orders/{order_id}/tasks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def add_task(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
    name: str = Form(...),
    assigned_to: str | None = Form(default=None),
    notes: str | None = Form(default=None),
) -> RedirectResponse:
    payload = ProvisioningTaskCreate(
        service_order_id=order_id,
        name=name,
        assigned_to=assigned_to or None,
        notes=notes or None,
    )
    task = provisioning_service.provisioning_tasks.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={
            "task_id": str(task.id),
            "name": name,
            "assigned_to": assigned_to or None,
        },
    )
    return RedirectResponse(
        url=f"/admin/provisioning/orders/{order_id}", status_code=303
    )


@router.post(
    "/orders/{order_id}/tasks/{task_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def task_status_update(
    request: Request,
    order_id: UUID,
    task_id: UUID,
    db: Session = Depends(get_db),
    new_status: str = Form(...),
) -> RedirectResponse:
    before = provisioning_service.provisioning_tasks.get(db, str(task_id))
    before_state = model_to_dict(before) if before else None
    payload = ProvisioningTaskUpdate(status=TaskStatus(new_status))
    provisioning_service.provisioning_tasks.update(db, str(task_id), payload)
    after = provisioning_service.provisioning_tasks.get(db, str(task_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )
    return RedirectResponse(
        url=f"/admin/provisioning/orders/{order_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Run Workflow (on order)
# ---------------------------------------------------------------------------


@router.post(
    "/orders/{order_id}/run-workflow",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def run_workflow(
    request: Request,
    order_id: UUID,
    db: Session = Depends(get_db),
    workflow_id: str = Form(...),
) -> RedirectResponse:
    provisioning_service.service_orders.run_for_order(
        db, str(order_id), workflow_id
    )
    log_audit_event(
        db=db,
        request=request,
        action="run_workflow",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={"workflow_id": workflow_id},
    )
    return RedirectResponse(
        url=f"/admin/provisioning/orders/{order_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Workflows - List
# ---------------------------------------------------------------------------


@router.get(
    "/workflows",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def workflows_list(
    request: Request,
    db: Session = Depends(get_db),
    vendor: str | None = Query(default=None),
    active: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
) -> HTMLResponse:
    is_active: bool | None = None
    if active == "true":
        is_active = True
    elif active == "false":
        is_active = False

    offset = (page - 1) * per_page
    workflows = provisioning_service.provisioning_workflows.list(
        db,
        vendor=vendor,
        is_active=is_active,
        order_by="name",
        order_dir="asc",
        limit=per_page + 1,
        offset=offset,
    )
    has_next = len(workflows) > per_page
    workflows = workflows[:per_page]

    ctx = _ctx(request, db, "workflows")
    ctx.update(
        {
            "workflows": workflows,
            "vendors": [v.value for v in ProvisioningVendor],
            "current_vendor": vendor,
            "current_active": active,
            "page": page,
            "per_page": per_page,
            "has_next": has_next,
            "has_prev": page > 1,
        }
    )
    return templates.TemplateResponse(
        "admin/provisioning/workflows/index.html", ctx
    )


# ---------------------------------------------------------------------------
# Workflows - Create
# ---------------------------------------------------------------------------


@router.get(
    "/workflows/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def workflow_create_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ctx = _ctx(request, db, "workflows")
    ctx.update(
        {
            "workflow": None,
            "vendors": [v.value for v in ProvisioningVendor],
        }
    )
    return templates.TemplateResponse(
        "admin/provisioning/workflows/form.html", ctx
    )


@router.post(
    "/workflows",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def workflow_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    vendor: str = Form(default="other"),
    description: str | None = Form(default=None),
    is_active: bool = Form(default=True),
) -> RedirectResponse:
    payload = ProvisioningWorkflowCreate(
        name=name,
        vendor=ProvisioningVendor(vendor),
        description=description or None,
        is_active=is_active,
    )
    workflow = provisioning_service.provisioning_workflows.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="provisioning_workflow",
        entity_id=str(workflow.id),
        actor_id=_actor_id(request),
        metadata={"name": name, "vendor": vendor},
    )
    return RedirectResponse(
        url=f"/admin/provisioning/workflows/{workflow.id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Workflows - Detail
# ---------------------------------------------------------------------------


@router.get(
    "/workflows/{workflow_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def workflow_detail(
    request: Request,
    workflow_id: UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    workflow = provisioning_service.provisioning_workflows.get(
        db, str(workflow_id)
    )
    if not workflow:
        ctx = _ctx(request, db, "workflows")
        return templates.TemplateResponse("admin/errors/404.html", ctx, status_code=404)

    steps = provisioning_service.provisioning_steps.list(
        db,
        workflow_id=str(workflow_id),
        step_type=None,
        is_active=True,
        order_by="order_index",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    ctx = _ctx(request, db, "workflows")
    ctx.update(
        {
            "workflow": workflow,
            "activities": build_audit_activities(
                db, "provisioning_workflow", str(workflow_id), limit=10
            ),
            "steps": steps,
            "step_types": [t.value for t in ProvisioningStepType],
            "vendors": [v.value for v in ProvisioningVendor],
        }
    )
    return templates.TemplateResponse(
        "admin/provisioning/workflows/detail.html", ctx
    )


# ---------------------------------------------------------------------------
# Workflows - Edit
# ---------------------------------------------------------------------------


@router.get(
    "/workflows/{workflow_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def workflow_edit_form(
    request: Request,
    workflow_id: UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    workflow = provisioning_service.provisioning_workflows.get(
        db, str(workflow_id)
    )
    if not workflow:
        ctx = _ctx(request, db, "workflows")
        return templates.TemplateResponse("admin/errors/404.html", ctx, status_code=404)

    ctx = _ctx(request, db, "workflows")
    ctx.update(
        {
            "workflow": workflow,
            "vendors": [v.value for v in ProvisioningVendor],
        }
    )
    return templates.TemplateResponse(
        "admin/provisioning/workflows/form.html", ctx
    )


@router.post(
    "/workflows/{workflow_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def workflow_edit(
    request: Request,
    workflow_id: UUID,
    db: Session = Depends(get_db),
    name: str = Form(...),
    vendor: str = Form(default="other"),
    description: str | None = Form(default=None),
    is_active: bool = Form(default=True),
) -> RedirectResponse:
    before = provisioning_service.provisioning_workflows.get(
        db, str(workflow_id)
    )
    before_state = model_to_dict(before) if before else None
    payload = ProvisioningWorkflowUpdate(
        name=name,
        vendor=ProvisioningVendor(vendor),
        description=description or None,
        is_active=is_active,
    )
    provisioning_service.provisioning_workflows.update(
        db, str(workflow_id), payload
    )
    after = provisioning_service.provisioning_workflows.get(
        db, str(workflow_id)
    )
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )
    return RedirectResponse(
        url=f"/admin/provisioning/workflows/{workflow_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Workflow Steps
# ---------------------------------------------------------------------------


@router.post(
    "/workflows/{workflow_id}/steps",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def add_step(
    request: Request,
    workflow_id: UUID,
    db: Session = Depends(get_db),
    name: str = Form(...),
    step_type: str = Form(...),
    order_index: int = Form(default=0),
    config_json: str | None = Form(default=None),
) -> RedirectResponse:
    config: dict | None = None
    if config_json:
        try:
            config = json.loads(config_json)
        except (json.JSONDecodeError, TypeError):
            config = None

    payload = ProvisioningStepCreate(
        workflow_id=workflow_id,
        name=name,
        step_type=ProvisioningStepType(step_type),
        order_index=order_index,
        config=config,
    )
    step = provisioning_service.provisioning_steps.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create_step",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata={
            "step_id": str(step.id),
            "name": name,
            "step_type": step_type,
            "order_index": order_index,
        },
    )
    return RedirectResponse(
        url=f"/admin/provisioning/workflows/{workflow_id}", status_code=303
    )


@router.post(
    "/workflows/{workflow_id}/steps/{step_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def edit_step(
    request: Request,
    workflow_id: UUID,
    step_id: UUID,
    db: Session = Depends(get_db),
    name: str | None = Form(default=None),
    step_type: str | None = Form(default=None),
    order_index: int | None = Form(default=None),
    config_json: str | None = Form(default=None),
) -> RedirectResponse:
    before = provisioning_service.provisioning_steps.get(db, str(step_id))
    before_state = model_to_dict(before) if before else None
    update_data: dict[str, object] = {}
    if name:
        update_data["name"] = name
    if step_type:
        update_data["step_type"] = ProvisioningStepType(step_type)
    if order_index is not None:
        update_data["order_index"] = order_index
    if config_json:
        try:
            update_data["config"] = json.loads(config_json)
        except (json.JSONDecodeError, TypeError):
            pass

    payload = ProvisioningStepUpdate.model_validate(update_data)
    provisioning_service.provisioning_steps.update(db, str(step_id), payload)
    after = provisioning_service.provisioning_steps.get(db, str(step_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update_step",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )
    return RedirectResponse(
        url=f"/admin/provisioning/workflows/{workflow_id}", status_code=303
    )


@router.post(
    "/workflows/{workflow_id}/steps/{step_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_step(
    request: Request,
    workflow_id: UUID,
    step_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    step = provisioning_service.provisioning_steps.get(db, str(step_id))
    payload = ProvisioningStepUpdate(is_active=False)
    provisioning_service.provisioning_steps.update(db, str(step_id), payload)
    log_audit_event(
        db=db,
        request=request,
        action="delete_step",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata={"step_id": str(step_id), "name": getattr(step, "name", None)},
    )
    return RedirectResponse(
        url=f"/admin/provisioning/workflows/{workflow_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Appointments - Global List
# ---------------------------------------------------------------------------


@router.get(
    "/appointments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def appointments_list(
    request: Request,
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    technician: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
) -> HTMLResponse:
    offset = (page - 1) * per_page
    appointments = provisioning_service.install_appointments.list(
        db,
        service_order_id=None,
        status=status,
        order_by="scheduled_start",
        order_dir="desc",
        limit=per_page + 1,
        offset=offset,
    )
    has_next = len(appointments) > per_page
    appointments = appointments[:per_page]

    ctx = _ctx(request, db, "appointments")
    ctx.update(
        {
            "appointments": appointments,
            "appointment_statuses": [s.value for s in AppointmentStatus],
            "current_status": status,
            "current_technician": technician or "",
            "page": page,
            "per_page": per_page,
            "has_next": has_next,
            "has_prev": page > 1,
            "subscriber_label": _subscriber_label,
        }
    )
    return templates.TemplateResponse(
        "admin/provisioning/appointments.html", ctx
    )


# ---------------------------------------------------------------------------
# Appointments - Status Update
# ---------------------------------------------------------------------------


@router.post(
    "/appointments/{appointment_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def appointment_status(
    request: Request,
    appointment_id: UUID,
    db: Session = Depends(get_db),
    new_status: str = Form(...),
    redirect_to: str | None = Form(default=None),
) -> RedirectResponse:
    before = provisioning_service.install_appointments.get(db, str(appointment_id))
    before_state = model_to_dict(before) if before else None
    payload = InstallAppointmentUpdate(
        status=AppointmentStatus(new_status)
    )
    provisioning_service.install_appointments.update(
        db, str(appointment_id), payload
    )
    after = provisioning_service.install_appointments.get(db, str(appointment_id))
    order_id = str(
        (after and getattr(after, "service_order_id", None))
        or (before and getattr(before, "service_order_id", None))
        or ""
    )
    if order_id:
        metadata = None
        if before_state is not None and after:
            changes = diff_dicts(before_state, model_to_dict(after))
            metadata = {"changes": changes} if changes else None
        log_audit_event(
            db=db,
            request=request,
            action="update_appointment",
            entity_type="service_order",
            entity_id=order_id,
            actor_id=_actor_id(request),
            metadata=metadata,
        )
    if redirect_to:
        return RedirectResponse(url=redirect_to, status_code=303)
    return RedirectResponse(
        url="/admin/provisioning/appointments", status_code=303
    )
