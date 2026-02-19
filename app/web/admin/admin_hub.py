"""Admin hub web routes for access control and monitoring."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin_hub as web_admin_hub_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/admin-hub", tags=["web-admin-hub"])


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
    context = _base_context(request, db, active_page="admin-hub")
    context.update(web_admin_hub_service.get_admin_hub_counts(db))
    return templates.TemplateResponse("admin/system/admin_hub/index.html", context)
