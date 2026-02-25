"""Admin network wireless site survey routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/site-survey", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def site_survey_list(request: Request, db: Session = Depends(get_db)):
    """List wireless site surveys."""
    from app.services import wireless_survey as ws_service

    surveys = ws_service.wireless_surveys.list(db, limit=100)
    context = _base_context(request, db, active_page="site-survey")
    context.update(
        {
            "surveys": surveys,
        }
    )
    return templates.TemplateResponse("admin/network/site-survey/index.html", context)


@router.get("/site-survey/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def site_survey_new(
    request: Request,
    lat: float | None = None,
    lon: float | None = None,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Create new wireless site survey page."""
    from app.services import wireless_survey as ws_service

    context = _base_context(request, db, active_page="site-survey")
    context.update(ws_service.wireless_surveys.build_form_context(db, None, lat, lon, subscriber_id))
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post("/site-survey/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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
    from app.services import wireless_survey as ws_service

    actor_id = getattr(request.state, "actor_id", None)
    survey = ws_service.wireless_surveys.create_from_form(
        db,
        name,
        description,
        frequency_mhz,
        default_antenna_height_m,
        default_tx_power_dbm,
        project_id,
        subscriber_id,
        actor_id,
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": survey.name},
    )
    redirect_url = ws_service.wireless_surveys.build_post_create_redirect(
        survey.id, initial_lat, initial_lon
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/site-survey/{survey_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def site_survey_detail(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Wireless site survey detail with interactive map."""
    from app.services import wireless_survey as ws_service

    context = _base_context(request, db, active_page="site-survey")
    context.update(ws_service.wireless_surveys.build_detail_context(db, survey_id))
    context["activities"] = build_audit_activities(db, "site_survey", str(survey_id), limit=10)
    return templates.TemplateResponse("admin/network/site-survey/detail.html", context)


@router.get("/site-survey/{survey_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def site_survey_edit(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Edit wireless site survey."""
    from app.services import wireless_survey as ws_service

    survey = ws_service.wireless_surveys.get(db, survey_id)
    context = _base_context(request, db, active_page="site-survey")
    context.update(ws_service.wireless_surveys.build_form_context(db, survey, None, None, None))
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post("/site-survey/{survey_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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
    from uuid import UUID

    from app.models.wireless_survey import SurveyStatus
    from app.schemas.wireless_survey import WirelessSiteSurveyUpdate
    from app.services import wireless_survey as ws_service

    existing_survey = ws_service.wireless_surveys.get(db, survey_id)
    before_snapshot = model_to_dict(existing_survey)
    payload = WirelessSiteSurveyUpdate(
        name=name,
        description=description,
        frequency_mhz=frequency_mhz,
        default_antenna_height_m=default_antenna_height_m,
        default_tx_power_dbm=default_tx_power_dbm,
        project_id=UUID(project_id) if project_id else None,
        status=SurveyStatus(status),
    )
    updated_survey = ws_service.wireless_surveys.update(db, survey_id, payload)
    after_snapshot = model_to_dict(updated_survey)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="site_survey",
        entity_id=str(updated_survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/{survey_id}/delete", dependencies=[Depends(require_permission("network:write"))])
def site_survey_delete(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Delete wireless site survey."""
    from app.services import wireless_survey as ws_service

    survey = ws_service.wireless_surveys.get(db, survey_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": survey.name},
    )
    ws_service.wireless_surveys.delete(db, survey_id)
    return RedirectResponse("/admin/network/site-survey", status_code=303)


@router.get("/site-survey/{survey_id}/elevation", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def site_survey_elevation_lookup(
    request: Request,
    survey_id: str,
    lat: float,
    lon: float,
    db: Session = Depends(get_db),
):
    """Get elevation for a point (HTMX endpoint)."""
    from app.services import dem as dem_service

    result = dem_service.get_elevation(lat, lon)
    return JSONResponse(result)


@router.post("/site-survey/{survey_id}/points", dependencies=[Depends(require_permission("network:write"))])
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
    from app.models.wireless_survey import SurveyPointType
    from app.schemas.wireless_survey import SurveyPointCreate
    from app.services import wireless_survey as ws_service

    payload = SurveyPointCreate(
        name=name,
        latitude=latitude,
        longitude=longitude,
        point_type=SurveyPointType(point_type),
        antenna_height_m=antenna_height_m,
    )
    point = ws_service.survey_points.create(db, survey_id, payload)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="point_added",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/points/{point_id}/delete", dependencies=[Depends(require_permission("network:write"))])
def site_survey_delete_point(request: Request, point_id: str, db: Session = Depends(get_db)):
    """Delete a survey point."""
    from app.services import wireless_survey as ws_service

    point = ws_service.survey_points.get(db, point_id)
    survey_id = point.survey_id
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="point_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    ws_service.survey_points.delete(db, point_id)
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/{survey_id}/analyze-los", dependencies=[Depends(require_permission("network:write"))])
def site_survey_analyze_los(
    request: Request,
    survey_id: str,
    from_point_id: str = Form(...),
    to_point_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Analyze LOS between two points."""
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.analyze_path(db, survey_id, from_point_id, to_point_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="los_analyzed",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
            "has_clear_los": los_path.has_clear_los,
        },
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.get("/site-survey/{survey_id}/los/{path_id}", dependencies=[Depends(require_permission("network:read"))])
def site_survey_los_detail(
    request: Request, survey_id: str, path_id: str, db: Session = Depends(get_db)
):
    """Get LOS path detail with elevation profile (JSON)."""
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.get(db, path_id)
    return JSONResponse(
        {
            "id": str(los_path.id),
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
            "distance_m": los_path.distance_m,
            "bearing_deg": los_path.bearing_deg,
            "has_clear_los": los_path.has_clear_los,
            "fresnel_clearance_pct": los_path.fresnel_clearance_pct,
            "max_obstruction_m": los_path.max_obstruction_m,
            "obstruction_distance_m": los_path.obstruction_distance_m,
            "free_space_loss_db": los_path.free_space_loss_db,
            "estimated_rssi_dbm": los_path.estimated_rssi_dbm,
            "elevation_profile": los_path.elevation_profile,
            "sample_count": los_path.sample_count,
        }
    )


@router.post("/site-survey/los/{path_id}/delete", dependencies=[Depends(require_permission("network:write"))])
def site_survey_delete_los(request: Request, path_id: str, db: Session = Depends(get_db)):
    """Delete a LOS path."""
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.get(db, path_id)
    survey_id = los_path.survey_id
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="los_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
        },
    )
    ws_service.survey_los.delete(db, path_id)
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)
