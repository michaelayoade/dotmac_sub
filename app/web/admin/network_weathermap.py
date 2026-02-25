"""Admin network weathermap routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_weathermap as web_network_weathermap_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/weathermap",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def network_weathermap(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    page_data = web_network_weathermap_service.build_weathermap_data(db)
    context = _base_context(request, db, active_page="weathermap")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/weathermap/index.html", context)
