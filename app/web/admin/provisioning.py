"""Admin provisioning management web routes."""

import logging
from datetime import datetime
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.provisioning import (
    AppointmentStatus,
    ProvisioningStepType,
    ProvisioningVendor,
    ServiceOrderStatus,
    TaskStatus,
)
from app.services import provisioning as provisioning_service
from app.services import web_admin as web_admin_service
from app.services import web_provisioning_actions as provisioning_actions_service
from app.services import web_provisioning_bulk_activate as bulk_activate_service
from app.services import web_provisioning_migration as migration_service
from app.services.audit_helpers import (
    build_audit_activities,
)
from app.services.auth_dependencies import require_permission
from app.tasks.provisioning import run_bulk_activation_job, run_service_migration_job
from app.web.request_parsing import parse_form_data_sync

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/provisioning", tags=["web-admin-provisioning"])


def _ctx(request: Request, db: Session, active_page: str = "provisioning") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


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
    return web_admin_service.get_actor_id(request)


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
def bulk_activate_preview(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = parse_form_data_sync(request)
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
def bulk_activate_execute(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.web.admin import get_current_user

    form = parse_form_data_sync(request)
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
        from app.celery_app import enqueue_celery_task

        enqueue_celery_task(
            run_bulk_activation_job,
            kwargs={"job_id": str(job["job_id"])},
            correlation_id=f"bulk_activation:{job['job_id']}",
            source="admin_provisioning_bulk_activate",
            request_id=getattr(request.state, "request_id", None),
            actor_id=actor_id,
        )
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
# Service Migration
# ---------------------------------------------------------------------------


@router.get(
    "/migrate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def service_migration_page(
    request: Request,
    reseller_id: str | None = Query(default=None),
    pop_site_id: str | None = Query(default=None),
    subscriber_status: str | None = Query(default=None),
    current_offer_id: str | None = Query(default=None),
    current_nas_device_id: str | None = Query(default=None),
    query: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ctx = _ctx(request, db, "provisioning-migrate")
    actor_id = _actor_id(request)
    filters = migration_service.MigrationFilters(
        reseller_id=reseller_id,
        pop_site_id=pop_site_id,
        subscriber_status=subscriber_status,
        current_offer_id=current_offer_id,
        current_nas_device_id=current_nas_device_id,
        query=query,
    )
    table = migration_service.build_selection_table(db, filters=filters)
    options = migration_service.page_options(db, actor_id=actor_id)
    active_job = (
        migration_service.get_job(db, job_id, actor_id=actor_id) if job_id else None
    )
    ctx.update(
        {
            **options,
            **table,
            "filters": filters,
            "active_job_id": job_id,
            "active_job": active_job,
            "preview": None,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse("admin/provisioning/migrate.html", ctx)


@router.post(
    "/migrate/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def service_migration_preview(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = parse_form_data_sync(request)
    filters = migration_service.parse_filters(dict(form))
    targets = migration_service.parse_targets(dict(form))
    selected_ids = migration_service.parse_selected_ids(form)
    preview = migration_service.build_preview(
        db,
        filters=filters,
        targets=targets,
        selected_ids=selected_ids,
    )
    return templates.TemplateResponse(
        "admin/provisioning/_migrate_preview.html",
        {"request": request, "preview": preview},
    )


@router.post(
    "/migrate/execute",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def service_migration_execute(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.web.admin import get_current_user

    form = parse_form_data_sync(request)
    filters = migration_service.parse_filters(dict(form))
    targets = migration_service.parse_targets(dict(form))
    selected_ids = migration_service.parse_selected_ids(form)
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id") or "").strip() or None

    try:
        job = migration_service.create_job(
            db,
            filters=filters,
            targets=targets,
            selected_ids=selected_ids,
            actor_id=actor_id,
        )
        scheduled_at = job.get("scheduled_at")
        if scheduled_at:
            eta = datetime.fromisoformat(str(scheduled_at))
            from app.celery_app import enqueue_celery_task

            enqueue_celery_task(
                run_service_migration_job,
                kwargs={"job_id": str(job["job_id"])},
                eta=eta,
                correlation_id=f"service_migration:{job['job_id']}",
                source="admin_provisioning_migration",
                request_id=getattr(request.state, "request_id", None),
                actor_id=actor_id,
            )
            notice = quote_plus("Service migration scheduled.")
        else:
            from app.celery_app import enqueue_celery_task

            enqueue_celery_task(
                run_service_migration_job,
                kwargs={"job_id": str(job["job_id"])},
                correlation_id=f"service_migration:{job['job_id']}",
                source="admin_provisioning_migration",
                request_id=getattr(request.state, "request_id", None),
                actor_id=actor_id,
            )
            notice = quote_plus("Service migration queued.")
        return RedirectResponse(
            url=f"/admin/provisioning/migrate?job_id={job['job_id']}&notice={notice}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/admin/provisioning/migrate?error={quote_plus(str(exc))}",
            status_code=303,
        )


@router.get(
    "/migrate/jobs/{job_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def service_migration_job_status(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = migration_service.get_job(db, job_id, actor_id=_actor_id(request))
    return templates.TemplateResponse(
        "admin/provisioning/_migrate_job_status.html",
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
        return templates.TemplateResponse("admin/provisioning/_table.html", ctx)
    return templates.TemplateResponse(
        "admin/provisioning/index.html", {**ctx, "show_orders": True}
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
    if not provisioning_actions_service.add_order_comment_with_mentions(
        db,
        request,
        order_id=order_id,
        comment=comment,
    ):
        return RedirectResponse(url="/admin/provisioning/orders", status_code=303)
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
    provisioning_actions_service.update_order_status_with_audit(
        db,
        request,
        order_id=order_id,
        new_status=new_status,
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
    provisioning_actions_service.add_appointment_with_audit(
        db,
        request,
        order_id=order_id,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        technician=technician,
        notes=notes,
        is_self_install=is_self_install,
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
    provisioning_actions_service.add_task_with_audit(
        db,
        request,
        order_id=order_id,
        name=name,
        assigned_to=assigned_to,
        notes=notes,
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
    provisioning_actions_service.update_task_status_with_audit(
        db,
        request,
        order_id=order_id,
        task_id=task_id,
        new_status=new_status,
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
    provisioning_actions_service.run_order_workflow_with_audit(
        db,
        request,
        order_id=order_id,
        workflow_id=workflow_id,
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
    return templates.TemplateResponse("admin/provisioning/workflows/index.html", ctx)


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
    return templates.TemplateResponse("admin/provisioning/workflows/form.html", ctx)


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
    workflow = provisioning_actions_service.create_workflow_with_audit(
        db,
        request,
        name=name,
        vendor=vendor,
        description=description,
        is_active=is_active,
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
    workflow = provisioning_service.provisioning_workflows.get(db, str(workflow_id))
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
    return templates.TemplateResponse("admin/provisioning/workflows/detail.html", ctx)


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
    workflow = provisioning_service.provisioning_workflows.get(db, str(workflow_id))
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
    return templates.TemplateResponse("admin/provisioning/workflows/form.html", ctx)


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
    provisioning_actions_service.update_workflow_with_audit(
        db,
        request,
        workflow_id=workflow_id,
        name=name,
        vendor=vendor,
        description=description,
        is_active=is_active,
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
    provisioning_actions_service.create_step_with_audit(
        db,
        request,
        workflow_id=workflow_id,
        name=name,
        step_type=step_type,
        order_index=order_index,
        config_json=config_json,
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
    provisioning_actions_service.update_step_with_audit(
        db,
        request,
        workflow_id=workflow_id,
        step_id=step_id,
        name=name,
        step_type=step_type,
        order_index=order_index,
        config_json=config_json,
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
    provisioning_actions_service.delete_step_with_audit(
        db,
        request,
        workflow_id=workflow_id,
        step_id=step_id,
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
    return templates.TemplateResponse("admin/provisioning/appointments.html", ctx)


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
    provisioning_actions_service.update_appointment_status_with_audit(
        db,
        request,
        appointment_id=appointment_id,
        new_status=new_status,
    )
    if redirect_to:
        return RedirectResponse(url=redirect_to, status_code=303)
    return RedirectResponse(url="/admin/provisioning/appointments", status_code=303)
