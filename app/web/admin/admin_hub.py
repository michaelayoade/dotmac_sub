"""Admin hub web routes for access control and monitoring."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.models.auth import ApiKey, UserCredential
from app.models.integration import IntegrationJob
from app.models.rbac import Role
from app.models.scheduler import ScheduledTask

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/admin-hub", tags=["web-admin-hub"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str):
    from app.web.admin import get_sidebar_stats, get_current_user

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("", response_class=HTMLResponse)
def admin_hub_index(request: Request, db: Session = Depends(get_db)):
    """Admin hub overview with cards for access control and monitoring."""
    # Access control counts
    users_count = db.query(UserCredential).count()
    users_active_count = db.query(UserCredential).filter(
        UserCredential.is_active.is_(True)
    ).count()
    roles_count = db.query(Role).count()
    api_keys_count = db.query(ApiKey).count()
    api_keys_active_count = db.query(ApiKey).filter(
        ApiKey.is_active.is_(True)
    ).count()

    # Monitoring counts
    scheduled_tasks_count = db.query(ScheduledTask).count()
    integration_jobs_count = db.query(IntegrationJob).count()

    context = _base_context(request, db, active_page="admin-hub")
    context.update({
        # Access Control
        "users_count": users_count,
        "users_active_count": users_active_count,
        "roles_count": roles_count,
        "api_keys_count": api_keys_count,
        "api_keys_active_count": api_keys_active_count,
        # Monitoring
        "scheduled_tasks_count": scheduled_tasks_count,
        "integration_jobs_count": integration_jobs_count,
    })
    return templates.TemplateResponse("admin/system/admin_hub/index.html", context)
