"""Admin network speed profile catalog web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_speed_profiles as web_speed_profiles_service
from app.services.auth_dependencies import require_permission
from app.services.network.speed_profiles import speed_profiles
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network/speed-profiles", tags=["web-admin-speed-profiles"])


def _base_context(
    request: Request,
    db: Session,
    active_page: str = "speed-profiles",
    active_menu: str = "network",
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def speed_profiles_list(
    request: Request,
    direction: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List speed profiles with download/upload tabs."""
    context = web_speed_profiles_service.list_context(
        request,
        db,
        direction=direction,
        search=search,
    )
    return templates.TemplateResponse(
        "admin/network/speed-profiles/index.html",
        context,
    )


@router.get(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def speed_profile_create_form(
    request: Request,
    direction: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show speed profile creation form."""
    context = web_speed_profiles_service.form_context(
        request,
        db,
        direction=direction,
    )
    return templates.TemplateResponse(
        "admin/network/speed-profiles/form.html",
        context,
    )


@router.post(
    "/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def speed_profile_create(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Handle speed profile creation."""
    form = parse_form_data_sync(request)
    values = web_speed_profiles_service.parse_form_values(form)
    error = web_speed_profiles_service.validate_form(values)

    if error:
        context = web_speed_profiles_service.form_context(
            request,
            db,
            error=error,
            form_values=values,
        )
        return templates.TemplateResponse(
            "admin/network/speed-profiles/form.html",
            context,
        )

    direction_enum = web_speed_profiles_service.handle_create(db, values)
    return RedirectResponse(
        url=f"/admin/network/speed-profiles?direction={direction_enum.value}",
        status_code=303,
    )


@router.get(
    "/{profile_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def speed_profile_edit_form(
    request: Request,
    profile_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show speed profile edit form."""
    try:
        context = web_speed_profiles_service.form_context(
            request,
            db,
            profile_id=profile_id,
        )
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Speed profile not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/network/speed-profiles/form.html",
        context,
    )


@router.post(
    "/{profile_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def speed_profile_update(
    request: Request,
    profile_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Handle speed profile update."""
    # Verify the profile exists
    try:
        speed_profiles.get(db, profile_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Speed profile not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    values = web_speed_profiles_service.parse_form_values(form)
    error = web_speed_profiles_service.validate_form(values)

    if error:
        context = web_speed_profiles_service.form_context(
            request,
            db,
            profile_id=profile_id,
            error=error,
            form_values=values,
        )
        return templates.TemplateResponse(
            "admin/network/speed-profiles/form.html",
            context,
        )

    direction_enum = web_speed_profiles_service.handle_update(db, profile_id, values)
    return RedirectResponse(
        url=f"/admin/network/speed-profiles?direction={direction_enum.value}",
        status_code=303,
    )


@router.post(
    "/{profile_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def speed_profile_delete(
    request: Request,
    profile_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Handle speed profile soft-delete."""
    try:
        profile = speed_profiles.get(db, profile_id)
        direction_value = profile.direction.value
        speed_profiles.delete(db, profile_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Speed profile not found"},
            status_code=404,
        )
    return RedirectResponse(
        url=f"/admin/network/speed-profiles?direction={direction_value}",
        status_code=303,
    )
