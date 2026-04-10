"""Design system reference page — living component library."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/design-system", tags=["web-admin-design-system"])

templates = Jinja2Templates(directory="templates")


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:read"))],
)
def design_system_index(request: Request):
    """Design system reference page with all UI components."""
    from app.web.admin import get_current_user

    return templates.TemplateResponse(
        "admin/design_system/index.html",
        {
            "request": request,
            "active_page": "design-system",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": {"app_name": "DotMac Subs"},
        },
    )


@router.get("/modules", response_class=HTMLResponse)
def design_system_modules(request: Request):
    """Module map — every page in the system grouped by function."""
    from app.web.admin import get_current_user

    return templates.TemplateResponse(
        "admin/design_system/modules.html",
        {
            "request": request,
            "active_page": "design-system",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": {"app_name": "DotMac Subs"},
        },
    )
