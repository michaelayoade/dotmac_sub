"""Admin dashboard web routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin_dashboard as web_admin_dashboard_service
from app.services import worker_control as worker_control_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_any_permission, require_permission

router = APIRouter(tags=["web-admin-dashboard"])

_DASHBOARD_READ_DEPENDENCY = Depends(
    require_any_permission("billing:invoice:read", "monitoring:read", "customer:read")
)


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    # The dashboard is the default staff landing page. Allow any staff with a
    # granular read permission to see the overview.
    dependencies=[_DASHBOARD_READ_DEPENDENCY],
)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Admin dashboard overview page."""
    return web_admin_dashboard_service.dashboard(request, db)


@router.get(
    "/dashboard/stats",
    response_class=HTMLResponse,
    dependencies=[_DASHBOARD_READ_DEPENDENCY],
)
def dashboard_stats_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for dashboard stats cards."""
    return web_admin_dashboard_service.dashboard_stats_partial(request, db)


@router.get(
    "/dashboard/activity",
    response_class=HTMLResponse,
    dependencies=[_DASHBOARD_READ_DEPENDENCY],
)
def dashboard_activity_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for recent activity feed."""
    return web_admin_dashboard_service.dashboard_activity_partial(request, db)


@router.get(
    "/dashboard/server-health",
    response_class=HTMLResponse,
    dependencies=[_DASHBOARD_READ_DEPENDENCY],
)
def dashboard_server_health_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for server health widget."""
    return web_admin_dashboard_service.dashboard_server_health_partial(request, db)


@router.post(
    "/dashboard/workers/restart",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def dashboard_worker_restart(
    request: Request,
    target: str = Form(...),
    db: Session = Depends(get_db),
):
    """Restart a configured worker service target and refresh the health widget."""
    result = worker_control_service.restart_worker_target(target)
    log_audit_event(
        db=db,
        request=request,
        action="restart_worker",
        entity_type="celery_worker",
        entity_id=result.target,
        actor_id=getattr(request.state, "actor_id", None),
        metadata={
            "target": result.target,
            "ok": result.ok,
            "message": result.message,
            "returncode": result.returncode,
        },
        status_code=200 if result.ok else 400,
        is_success=result.ok,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
    web_admin_dashboard_service.clear_dashboard_infrastructure_cache()
    notice = {
        "type": "success" if result.ok else "error",
        "message": result.message,
    }
    return web_admin_dashboard_service.dashboard_server_health_partial(
        request, db, worker_action_notice=notice
    )
