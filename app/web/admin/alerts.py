"""Admin operational alerts routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import admin_alerts as admin_alerts_service
from app.services.auth_dependencies import require_any_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/alerts", tags=["web-admin-alerts"])

_ALERT_ACCESS = Depends(
    require_any_permission("system:read", "system:settings:read", "monitoring:read")
)


@router.get("", response_class=HTMLResponse, dependencies=[_ALERT_ACCESS])
def alerts_index(
    request: Request,
    category: str | None = Query(None),
    status: str | None = Query("open"),
    severity: str | None = Query(None),
    source: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/alerts/index.html",
        {
            "request": request,
            **admin_alerts_service.alerts_context(
                db,
                category=category,
                status=status,
                severity=severity,
                source=source,
                page=page,
                per_page=per_page,
            ),
            "active_page": "admin-alerts",
            "active_menu": "dashboard",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/notifications/{notification_id}/open",
    dependencies=[_ALERT_ACCESS],
)
def open_notification(
    notification_id: UUID,
    db: Session = Depends(get_db),
):
    notification = admin_alerts_service.mark_notification_read(db, str(notification_id))
    if notification is None:
        return RedirectResponse(url="/admin/alerts", status_code=303)
    return RedirectResponse(
        url=notification.target_url or "/admin/alerts", status_code=303
    )


@router.post(
    "/{alert_id}/acknowledge",
    dependencies=[_ALERT_ACCESS],
)
def acknowledge_alert(
    alert_id: UUID,
    db: Session = Depends(get_db),
):
    admin_alerts_service.acknowledge_alert(db, str(alert_id))
    return RedirectResponse(url="/admin/alerts", status_code=303)


@router.post(
    "/{alert_id}/resolve",
    dependencies=[_ALERT_ACCESS],
)
def resolve_alert(
    alert_id: UUID,
    db: Session = Depends(get_db),
):
    admin_alerts_service.resolve_alert(db, str(alert_id))
    return RedirectResponse(url="/admin/alerts", status_code=303)
