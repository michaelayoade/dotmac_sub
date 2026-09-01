"""Admin operational alerts routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import admin_alerts as admin_alerts_service
from app.services import staff_notification_read_state
from app.services.auth_dependencies import require_any_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/alerts", tags=["web-admin-alerts"])

_ALERT_ACCESS = Depends(
    require_any_permission("system:read", "system:settings:read", "monitoring:read")
)

# Acknowledging or resolving an alert changes its durable state, so it must not
# ride the read guard above: ``monitoring:read`` is UI-assignable and held by
# the seeded ``operator`` role, which made "may look at alerts" and "may close
# alerts" the same grant. This is the same any-of shape at the write tier, so
# every domain that could read alerts can still act on them with its own write
# grant. (``system:write``/``system:settings:write`` are admin-only in the
# seeded catalogue; ``monitoring:write`` is the assignable one today.)
_ALERT_ACT = Depends(
    require_any_permission("system:write", "system:settings:write", "monitoring:write")
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
)
def open_notification(
    notification_id: UUID,
    db: Session = Depends(get_db),
    auth: dict = _ALERT_ACCESS,
):
    if auth.get("principal_type") != "system_user":
        return RedirectResponse(url="/admin/alerts", status_code=303)
    try:
        system_user_id = UUID(str(auth.get("principal_id")))
    except (TypeError, ValueError):
        return RedirectResponse(url="/admin/alerts", status_code=303)
    db_session_adapter.release_read_transaction(db)
    outcome = staff_notification_read_state.open_staff_notification(
        db,
        staff_notification_read_state.OpenStaffNotification(
            notification_id=notification_id,
            system_user_id=system_user_id,
            context=CommandContext.system(
                actor=f"system_user:{system_user_id}",
                scope="communications:staff_notifications",
                reason="Open personal staff notification from alerts",
                idempotency_key=(
                    f"staff-notification-open:{system_user_id}:{notification_id}"
                ),
            ),
        ),
    )
    if not outcome.opened or outcome.target_url is None:
        return RedirectResponse(url="/admin/alerts", status_code=303)
    return RedirectResponse(url=outcome.target_url, status_code=303)


@router.post(
    "/{alert_id}/acknowledge",
    dependencies=[_ALERT_ACT],
)
def acknowledge_alert(
    alert_id: UUID,
    db: Session = Depends(get_db),
):
    admin_alerts_service.acknowledge_alert(db, str(alert_id))
    return RedirectResponse(url="/admin/alerts", status_code=303)


@router.post(
    "/{alert_id}/resolve",
    dependencies=[_ALERT_ACT],
)
def resolve_alert(
    alert_id: UUID,
    db: Session = Depends(get_db),
):
    admin_alerts_service.resolve_alert(db, str(alert_id))
    return RedirectResponse(url="/admin/alerts", status_code=303)
