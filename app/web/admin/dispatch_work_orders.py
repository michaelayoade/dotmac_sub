"""Admin dispatch work-order routes."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_dispatch_work_orders as work_orders_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/dispatch", tags=["web-admin-dispatch"])


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "dispatch-work-orders",
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/work-orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch"))],
)
def dispatch_work_orders(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    notice: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    state = work_orders_service.list_page(
        db,
        status=status,
        q=q,
        page=page,
        per_page=per_page,
    )
    context = _ctx(request, db)
    context.update(state)
    context.update({"notice": notice, "error": error})
    return templates.TemplateResponse("admin/dispatch/work_orders.html", context)


@router.post(
    "/work-orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch"))],
)
def create_dispatch_work_order(
    request: Request,
    db: Session = Depends(get_db),
):
    form = dict(parse_form_data_sync(request))
    try:
        row = work_orders_service.create_from_form(db, form)
    except (HTTPException, ValidationError, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _redirect(error=detail)
    return _redirect(notice=f"Work order {row.crm_work_order_id} created")


@router.post(
    "/work-orders/{work_order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch"))],
)
def update_dispatch_work_order(
    request: Request,
    work_order_id: str,
    db: Session = Depends(get_db),
):
    form = dict(parse_form_data_sync(request))
    try:
        work_orders_service.update_from_form(db, work_order_id, form)
    except (HTTPException, ValidationError, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _redirect(error=detail)
    return _redirect(notice=f"Work order {work_order_id} updated")


@router.post(
    "/work-orders/{work_order_id}/queue",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch"))],
)
def queue_dispatch_work_order(
    work_order_id: str,
    assigned_technician_id: str = Form(...),
    status: str = Form("queued"),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        work_orders_service.queue_assignment_from_form(
            db,
            work_order_id,
            {
                "assigned_technician_id": assigned_technician_id,
                "status": status,
                "reason": reason,
            },
        )
    except (HTTPException, ValidationError, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _redirect(error=detail)
    return _redirect(notice=f"Work order {work_order_id} queued")


def _redirect(
    *, notice: str | None = None, error: str | None = None
) -> RedirectResponse:
    url = "/admin/dispatch/work-orders"
    if notice:
        url += f"?notice={quote(str(notice))}"
    elif error:
        url += f"?error={quote(str(error))}"
    return RedirectResponse(url=url, status_code=303)
