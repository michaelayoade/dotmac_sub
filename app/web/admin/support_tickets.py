"""Admin support tickets routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_support_tickets as support_web_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/support/tickets", tags=["web-admin-support-tickets"])
templates = Jinja2Templates(directory="templates")


def _ctx(request: Request, db: Session, active_page: str = "support-tickets") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    value = (
        current_user.get("actor_id") or current_user.get("subscriber_id")
        if current_user
        else None
    )
    return str(value) if value else None


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def tickets_list(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    assigned_to_me: bool = Query(default=False),
    project_manager_person_id: str | None = Query(default=None),
    site_coordinator_person_id: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    context.update(
        support_web_service.build_tickets_list_context(
            db,
            search=search,
            status=status,
            ticket_type=ticket_type,
            assigned_to_me=assigned_to_me,
            actor_id=_actor_id(request),
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            order_by=order_by,
            order_dir=order_dir,
            page=page,
            per_page=per_page,
            visible_columns_cookie=request.cookies.get("ticket_columns"),
        )
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("admin/support/tickets/_table.html", context)
    return templates.TemplateResponse("admin/support/tickets/index.html", context)


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def ticket_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update(
        support_web_service.build_ticket_form_context(
            db, query_params=request.query_params
        )
    )
    context.update({"page_title": "New Ticket", "form_mode": "create", "ticket": None})
    return templates.TemplateResponse("admin/support/tickets/new.html", context)


@router.get(
    "/{ticket_lookup}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_edit_page(
    request: Request, ticket_lookup: str, db: Session = Depends(get_db)
):
    context = _ctx(request, db)
    context.update(
        support_web_service.build_ticket_edit_page_context(
            db,
            query_params=request.query_params,
            ticket_lookup=ticket_lookup,
        )
    )
    return templates.TemplateResponse("admin/support/tickets/new.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def ticket_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    subscriber_id: str | None = Form(default=None),
    customer_account_id: str | None = Form(default=None),
    customer_person_id: str | None = Form(default=None),
    region: str | None = Form(default=None),
    technician_person_id: str | None = Form(default=None),
    ticket_manager_person_id: str | None = Form(default=None),
    site_coordinator_person_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    ticket_type: str | None = Form(default=None),
    priority: str = Form("normal"),
    channel: str = Form("web"),
    status: str = Form("open"),
    due_at: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    related_outage_ticket_id: str | None = Form(default=None),
    assignee_person_ids: list[str] = Form(default=[]),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id(request)
    ticket = support_web_service.create_ticket_from_form(
        db,
        request=request,
        actor_id=actor_id,
        attachments=attachments,
        title=title,
        description=description,
        subscriber_id=subscriber_id,
        customer_account_id=customer_account_id,
        customer_person_id=customer_person_id,
        region=region,
        technician_person_id=technician_person_id,
        ticket_manager_person_id=ticket_manager_person_id,
        site_coordinator_person_id=site_coordinator_person_id,
        service_team_id=service_team_id,
        ticket_type=ticket_type,
        priority=priority,
        channel=channel,
        status=status,
        due_at=due_at,
        tags=tags,
        related_outage_ticket_id=related_outage_ticket_id,
        assignee_person_ids=assignee_person_ids,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket.id}", status_code=303)


@router.get(
    "/{ticket_lookup}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def ticket_detail(request: Request, ticket_lookup: str, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update(
        support_web_service.build_ticket_detail_context(db, ticket_lookup=ticket_lookup)
    )
    return templates.TemplateResponse("admin/support/tickets/detail.html", context)


@router.post(
    "/{ticket_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_edit(
    request: Request,
    ticket_id: UUID,
    title: str = Form(...),
    description: str = Form(""),
    subscriber_id: str | None = Form(default=None),
    customer_account_id: str | None = Form(default=None),
    customer_person_id: str | None = Form(default=None),
    region: str | None = Form(default=None),
    status: str = Form("open"),
    priority: str = Form("normal"),
    channel: str = Form("web"),
    ticket_type: str | None = Form(default=None),
    due_at: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    technician_person_id: str | None = Form(default=None),
    ticket_manager_person_id: str | None = Form(default=None),
    site_coordinator_person_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    assignee_person_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    support_web_service.update_ticket_from_form(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
        title=title,
        description=description,
        subscriber_id=subscriber_id,
        customer_account_id=customer_account_id,
        customer_person_id=customer_person_id,
        region=region,
        status=status,
        priority=priority,
        channel=channel,
        ticket_type=ticket_type,
        due_at=due_at,
        tags=tags,
        technician_person_id=technician_person_id,
        ticket_manager_person_id=ticket_manager_person_id,
        site_coordinator_person_id=site_coordinator_person_id,
        service_team_id=service_team_id,
        assignee_person_ids=assignee_person_ids,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/comment",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_add_comment(
    request: Request,
    ticket_id: UUID,
    body: str = Form(...),
    is_internal: bool = Form(False),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id(request)
    support_web_service.add_ticket_comment_from_form(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=actor_id,
        body=body,
        is_internal=is_internal,
        attachments=attachments,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/auto-assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_auto_assign(
    request: Request, ticket_id: UUID, db: Session = Depends(get_db)
):
    support_web_service.auto_assign_ticket(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/link",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_link(
    request: Request,
    ticket_id: UUID,
    to_ticket_id: str = Form(...),
    link_type: str = Form(...),
    db: Session = Depends(get_db),
):
    support_web_service.link_ticket_from_form(
        db,
        request=request,
        ticket_id=str(ticket_id),
        to_ticket_id=to_ticket_id,
        link_type=link_type,
        actor_id=_actor_id(request),
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/merge",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_merge(
    request: Request,
    ticket_id: UUID,
    target_ticket_id: str = Form(...),
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    target = support_web_service.merge_ticket_from_form(
        db,
        request=request,
        ticket_id=str(ticket_id),
        target_ticket_id=target_ticket_id,
        reason=reason,
        actor_id=_actor_id(request),
    )
    return RedirectResponse(url=f"/admin/support/tickets/{target.id}", status_code=303)


@router.post(
    "/{ticket_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:delete"))],
)
def ticket_delete(request: Request, ticket_id: UUID, db: Session = Depends(get_db)):
    support_web_service.delete_ticket(
        db,
        request=request,
        ticket_id=str(ticket_id),
        actor_id=_actor_id(request),
    )
    if request.headers.get("HX-Request"):
        return Response(
            status_code=204,
            headers=support_web_service.delete_ticket_hx_headers(),
        )
    return RedirectResponse(url="/admin/support/tickets", status_code=303)


# Legacy path compatibility used by existing smoke tests.
legacy_router = APIRouter(prefix="/tickets", tags=["web-admin-support-tickets-legacy"])


@legacy_router.get("", response_class=HTMLResponse)
def legacy_ticket_index():
    return RedirectResponse(url="/admin/support/tickets", status_code=307)


@legacy_router.get("/{ticket_lookup}", response_class=HTMLResponse)
def legacy_ticket_detail(ticket_lookup: str):
    return RedirectResponse(
        url=f"/admin/support/tickets/{ticket_lookup}", status_code=307
    )
