"""Admin network wireless site survey routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_site_survey as site_survey_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


@router.get(
    "/site-survey",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def site_survey_list(request: Request, db: Session = Depends(get_db)):
    """List wireless site surveys."""
    context = site_survey_service.list_context(request, db)
    return templates.TemplateResponse("admin/network/site-survey/index.html", context)


@router.get(
    "/site-survey/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def site_survey_new(
    request: Request,
    lat: float | None = None,
    lon: float | None = None,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Create new wireless site survey page."""
    context = site_survey_service.new_context(
        request,
        db,
        lat=lat,
        lon=lon,
        subscriber_id=subscriber_id,
    )
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post(
    "/site-survey/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def site_survey_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(None),
    frequency_mhz: float = Form(None),
    default_antenna_height_m: float = Form(10.0),
    default_tx_power_dbm: float = Form(20.0),
    project_id: str = Form(None),
    initial_lat: float | None = Form(None),
    initial_lon: float | None = Form(None),
    subscriber_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create new wireless site survey."""
    redirect_url = site_survey_service.create_survey(
        request,
        db,
        name=name,
        description=description,
        frequency_mhz=frequency_mhz,
        default_antenna_height_m=default_antenna_height_m,
        default_tx_power_dbm=default_tx_power_dbm,
        project_id=project_id,
        subscriber_id=subscriber_id,
        initial_lat=initial_lat,
        initial_lon=initial_lon,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get(
    "/site-survey/{survey_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def site_survey_detail(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Wireless site survey detail with interactive map."""
    context = site_survey_service.detail_context(request, db, survey_id=survey_id)
    return templates.TemplateResponse("admin/network/site-survey/detail.html", context)


@router.get(
    "/site-survey/{survey_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def site_survey_edit(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Edit wireless site survey."""
    context = site_survey_service.edit_context(request, db, survey_id=survey_id)
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post(
    "/site-survey/{survey_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def site_survey_update(
    request: Request,
    survey_id: str,
    name: str = Form(...),
    description: str = Form(None),
    frequency_mhz: float = Form(None),
    default_antenna_height_m: float = Form(10.0),
    default_tx_power_dbm: float = Form(20.0),
    project_id: str = Form(None),
    status: str = Form("draft"),
    db: Session = Depends(get_db),
):
    """Update wireless site survey."""
    redirect_url = site_survey_service.update_survey(
        request,
        db,
        survey_id=survey_id,
        name=name,
        description=description,
        frequency_mhz=frequency_mhz,
        default_antenna_height_m=default_antenna_height_m,
        default_tx_power_dbm=default_tx_power_dbm,
        project_id=project_id,
        status=status,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.post(
    "/site-survey/{survey_id}/delete",
    dependencies=[Depends(require_permission("network:write"))],
)
def site_survey_delete(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Delete wireless site survey."""
    redirect_url = site_survey_service.delete_survey(request, db, survey_id=survey_id)
    return RedirectResponse(redirect_url, status_code=303)


@router.get(
    "/site-survey/{survey_id}/elevation",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def site_survey_elevation_lookup(
    request: Request,
    survey_id: str,
    lat: float,
    lon: float,
    db: Session = Depends(get_db),
):
    """Get elevation for a point (HTMX endpoint)."""
    result = site_survey_service.lookup_elevation(lat=lat, lon=lon)
    return JSONResponse(result)


@router.post(
    "/site-survey/{survey_id}/points",
    dependencies=[Depends(require_permission("network:write"))],
)
def site_survey_add_point(
    request: Request,
    survey_id: str,
    name: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    point_type: str = Form("custom"),
    antenna_height_m: float = Form(10.0),
    db: Session = Depends(get_db),
):
    """Add a point to a survey."""
    redirect_url = site_survey_service.add_point(
        request,
        db,
        survey_id=survey_id,
        name=name,
        latitude=latitude,
        longitude=longitude,
        point_type=point_type,
        antenna_height_m=antenna_height_m,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.post(
    "/site-survey/points/{point_id}/delete",
    dependencies=[Depends(require_permission("network:write"))],
)
def site_survey_delete_point(
    request: Request, point_id: str, db: Session = Depends(get_db)
):
    """Delete a survey point."""
    redirect_url = site_survey_service.delete_point(request, db, point_id=point_id)
    return RedirectResponse(redirect_url, status_code=303)


@router.post(
    "/site-survey/{survey_id}/analyze-los",
    dependencies=[Depends(require_permission("network:write"))],
)
def site_survey_analyze_los(
    request: Request,
    survey_id: str,
    from_point_id: str = Form(...),
    to_point_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Analyze LOS between two points."""
    redirect_url = site_survey_service.analyze_los(
        request,
        db,
        survey_id=survey_id,
        from_point_id=from_point_id,
        to_point_id=to_point_id,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get(
    "/site-survey/{survey_id}/los/{path_id}",
    dependencies=[Depends(require_permission("network:read"))],
)
def site_survey_los_detail(
    request: Request, survey_id: str, path_id: str, db: Session = Depends(get_db)
):
    """Get LOS path detail with elevation profile (JSON)."""
    los_payload = site_survey_service.los_detail(db, path_id=path_id)
    return JSONResponse(los_payload)


@router.post(
    "/site-survey/los/{path_id}/delete",
    dependencies=[Depends(require_permission("network:write"))],
)
def site_survey_delete_los(
    request: Request, path_id: str, db: Session = Depends(get_db)
):
    """Delete a LOS path."""
    redirect_url = site_survey_service.delete_los(request, db, path_id=path_id)
    return RedirectResponse(redirect_url, status_code=303)
