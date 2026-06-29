"""Admin queue for reseller service requests (new connections / installs)."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import reseller_service_requests
from app.services import web_service_requests as web_service_requests_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/service-requests", tags=["web-admin-service-requests"])


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def service_requests_list(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    state = web_service_requests_service.list_data(
        db, status=status, page=page, per_page=per_page
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/service_requests/index.html",
        {
            "request": request,
            **state,
            "active_page": "service-requests",
            "active_menu": "service-requests",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/{request_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def service_requests_detail(
    request: Request,
    request_id: str,
    error: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
):
    state = web_service_requests_service.detail_data(db, request_id=request_id)
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Service request not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/service_requests/detail.html",
        {
            "request": request,
            **state,
            "error": error,
            "message": message,
            "active_page": "service-requests",
            "active_menu": "service-requests",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/{request_id}/status", response_class=HTMLResponse)
def service_requests_update_status(
    request: Request,
    request_id: str,
    status: str = Form(...),
    admin_notes: str = Form(""),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("provisioning:write")),
):
    try:
        reseller_service_requests.update_status(
            db,
            request_id,
            status=status,
            admin_notes=admin_notes or None,
        )
    except HTTPException as exc:
        return _redirect(request_id, error=str(exc.detail))
    return _redirect(request_id, message="Service request updated")


def _redirect(
    request_id: str, *, error: str | None = None, message: str | None = None
) -> RedirectResponse:
    url = f"/admin/service-requests/{request_id}"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)
