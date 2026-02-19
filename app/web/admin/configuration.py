"""Admin configuration hub web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_configuration as web_configuration_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/configuration", tags=["web-admin-configuration"])


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
def configuration_index(request: Request, db: Session = Depends(get_db)):
    """Configuration overview with cards linking to each section."""
    context = _base_context(request, db, active_page="configuration")
    context.update(web_configuration_service.get_configuration_counts(db))
    return templates.TemplateResponse("admin/system/configuration/index.html", context)
