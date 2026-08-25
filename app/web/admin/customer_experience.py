"""Admin Customer Experience handoff queue."""

from __future__ import annotations

from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.customer_experience import CustomerExperienceHandoffStatus
from app.services import customer_experience_handoffs
from app.services import web_admin as web_admin_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/customer-experience",
    tags=["web-admin-customer-experience"],
)

_DEFAULT_STATUS = CustomerExperienceHandoffStatus.ready.value
_VISIBLE_STATUSES = [
    CustomerExperienceHandoffStatus.ready.value,
    CustomerExperienceHandoffStatus.needs_attention.value,
    CustomerExperienceHandoffStatus.pending.value,
    CustomerExperienceHandoffStatus.accepted.value,
]


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "customer-experience-handoffs",
        "active_menu": "customer-experience",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor_id(request: Request) -> str:
    return web_admin_service.get_actor_id(request) or "admin-customer-experience-user"


def _redirect(
    *,
    status: str | None,
    notice: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    params: dict[str, str] = {}
    if status:
        params["status"] = status
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    query = urlencode(params)
    url = "/admin/customer-experience/handoffs"
    if query:
        url = f"{url}?{query}"
    return RedirectResponse(url=url, status_code=303)


@router.get(
    "/handoffs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer_experience:handoff:read"))],
)
def handoff_queue(
    request: Request,
    status: str | None = Query(default=_DEFAULT_STATUS),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    notice: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    status_filter = (status or "").strip() or _DEFAULT_STATUS
    queue_status = None if status_filter == "all" else status_filter
    if queue_status not in {
        None,
        *[item.value for item in CustomerExperienceHandoffStatus],
    }:
        return _redirect(
            status=_DEFAULT_STATUS,
            error="That handoff status does not exist.",
        )

    offset = (page - 1) * per_page
    handoffs = customer_experience_handoffs.list_handoff_queue(
        db,
        status=queue_status,
        limit=per_page + 1,
        offset=offset,
    )
    has_next = len(handoffs) > per_page
    handoffs = handoffs[:per_page]
    counts = customer_experience_handoffs.count_handoff_queue(db)
    total = counts.get(queue_status or "all", 0)

    context = _ctx(request, db)
    context.update(
        {
            "page_title": "Customer Experience Handoffs",
            "handoffs": handoffs,
            "counts": counts,
            "visible_statuses": _VISIBLE_STATUSES,
            "status_filter": status_filter,
            "notice": notice,
            "error": error,
            "page": page,
            "per_page": per_page,
            "has_next": has_next,
            "has_prev": page > 1,
            "total": total,
        }
    )
    return templates.TemplateResponse(
        "admin/customer_experience/handoffs.html",
        context,
    )


@router.post(
    "/handoffs/{handoff_id}/accept",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer_experience:handoff:accept"))],
)
def accept_handoff(
    request: Request,
    handoff_id: UUID,
    status: str | None = Form(default=_DEFAULT_STATUS),
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        customer_experience_handoffs.accept_handoff(
            db,
            handoff_id=handoff_id,
            actor_type="staff_user",
            actor_id=_actor_id(request),
            reason=reason,
        )
    except customer_experience_handoffs.CustomerExperienceHandoffError as exc:
        return _redirect(status=status, error=str(exc))
    return _redirect(status=status, notice="Handoff accepted.")


@router.post(
    "/handoffs/{handoff_id}/needs-attention",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer_experience:handoff:attention"))],
)
def needs_attention(
    request: Request,
    handoff_id: UUID,
    status: str | None = Form(default=_DEFAULT_STATUS),
    reason: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        customer_experience_handoffs.mark_needs_attention(
            db,
            handoff_id=handoff_id,
            actor_type="staff_user",
            actor_id=_actor_id(request),
            reason=reason,
        )
    except customer_experience_handoffs.CustomerExperienceHandoffError as exc:
        return _redirect(status=status, error=str(exc))
    return _redirect(status=status, notice="Handoff marked for attention.")


@router.post(
    "/handoffs/{handoff_id}/resolve-attention",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer_experience:handoff:attention"))],
)
def resolve_attention(
    request: Request,
    handoff_id: UUID,
    status: str | None = Form(
        default=CustomerExperienceHandoffStatus.needs_attention.value
    ),
    reason: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        customer_experience_handoffs.resolve_attention(
            db,
            handoff_id=handoff_id,
            actor_type="staff_user",
            actor_id=_actor_id(request),
            reason=reason,
        )
    except customer_experience_handoffs.CustomerExperienceHandoffError as exc:
        return _redirect(status=status, error=str(exc))
    return _redirect(status=status, notice="Attention resolved.")
