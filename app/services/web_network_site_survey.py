"""Service helpers for admin network site survey routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.csrf import get_csrf_token
from app.services import dem as dem_service

try:
    from app.services import wireless_survey as ws_service
except Exception:  # pragma: no cover - fallback for isolated test imports
    ws_service = SimpleNamespace(
        wireless_surveys=SimpleNamespace(),
        survey_points=SimpleNamespace(),
        survey_los=SimpleNamespace(),
    )
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)


def _base_context(
    request: Request,
    db: Session,
    *,
    active_page: str = "site-survey",
    active_menu: str = "network",
) -> dict[str, Any]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    subscriber_id = current_user.get("subscriber_id")
    return str(subscriber_id) if subscriber_id else None


def list_context(request: Request, db: Session) -> dict[str, Any]:
    surveys = ws_service.wireless_surveys.list(db, limit=100)
    context = _base_context(request, db)
    context.update({"surveys": surveys})
    return context


def new_context(
    request: Request,
    db: Session,
    *,
    lat: float | None = None,
    lon: float | None = None,
    subscriber_id: str | None = None,
) -> dict[str, Any]:
    context = _base_context(request, db)
    context.update(ws_service.wireless_surveys.build_form_context(db, None, lat, lon, subscriber_id))
    return context


def create_survey(
    request: Request,
    db: Session,
    *,
    name: str,
    description: str | None,
    frequency_mhz: float | None,
    default_antenna_height_m: float,
    default_tx_power_dbm: float,
    project_id: str | None,
    subscriber_id: str | None,
    initial_lat: float | None,
    initial_lon: float | None,
) -> str:
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
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=_actor_id(request),
        metadata={"name": survey.name},
    )
    return ws_service.wireless_surveys.build_post_create_redirect(
        survey.id,
        initial_lat,
        initial_lon,
    )


def detail_context(request: Request, db: Session, *, survey_id: str) -> dict[str, Any]:
    context = _base_context(request, db)
    context.update(ws_service.wireless_surveys.build_detail_context(db, survey_id))
    context["activities"] = build_audit_activities(db, "site_survey", str(survey_id), limit=10)
    return context


def edit_context(request: Request, db: Session, *, survey_id: str) -> dict[str, Any]:
    survey = ws_service.wireless_surveys.get(db, survey_id)
    context = _base_context(request, db)
    context.update(ws_service.wireless_surveys.build_form_context(db, survey, None, None, None))
    return context


def update_survey(
    request: Request,
    db: Session,
    *,
    survey_id: str,
    name: str,
    description: str | None,
    frequency_mhz: float | None,
    default_antenna_height_m: float,
    default_tx_power_dbm: float,
    project_id: str | None,
    status: str,
) -> str:
    existing_survey = ws_service.wireless_surveys.get(db, survey_id)
    before_snapshot = model_to_dict(existing_survey)
    from app.models.wireless_survey import SurveyStatus
    from app.schemas.wireless_survey import WirelessSiteSurveyUpdate

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
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="site_survey",
        entity_id=str(updated_survey.id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )
    return f"/admin/network/site-survey/{survey_id}"


def delete_survey(request: Request, db: Session, *, survey_id: str) -> str:
    survey = ws_service.wireless_surveys.get(db, survey_id)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=_actor_id(request),
        metadata={"name": survey.name},
    )
    ws_service.wireless_surveys.delete(db, survey_id)
    return "/admin/network/site-survey"


def lookup_elevation(*, lat: float, lon: float) -> dict[str, Any]:
    return dem_service.get_elevation(lat, lon)


def add_point(
    request: Request,
    db: Session,
    *,
    survey_id: str,
    name: str,
    latitude: float,
    longitude: float,
    point_type: str,
    antenna_height_m: float,
) -> str:
    from app.models.wireless_survey import SurveyPointType
    from app.schemas.wireless_survey import SurveyPointCreate

    payload = SurveyPointCreate(
        name=name,
        latitude=latitude,
        longitude=longitude,
        point_type=SurveyPointType(point_type),
        antenna_height_m=antenna_height_m,
    )
    point = ws_service.survey_points.create(db, survey_id, payload)
    log_audit_event(
        db=db,
        request=request,
        action="point_added",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=_actor_id(request),
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    return f"/admin/network/site-survey/{survey_id}"


def delete_point(request: Request, db: Session, *, point_id: str) -> str:
    point = ws_service.survey_points.get(db, point_id)
    survey_id = point.survey_id
    log_audit_event(
        db=db,
        request=request,
        action="point_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=_actor_id(request),
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    ws_service.survey_points.delete(db, point_id)
    return f"/admin/network/site-survey/{survey_id}"


def analyze_los(
    request: Request,
    db: Session,
    *,
    survey_id: str,
    from_point_id: str,
    to_point_id: str,
) -> str:
    los_path = ws_service.survey_los.analyze_path(db, survey_id, from_point_id, to_point_id)
    log_audit_event(
        db=db,
        request=request,
        action="los_analyzed",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=_actor_id(request),
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
            "has_clear_los": los_path.has_clear_los,
        },
    )
    return f"/admin/network/site-survey/{survey_id}"


def los_detail(db: Session, *, path_id: str) -> dict[str, Any]:
    los_path = ws_service.survey_los.get(db, path_id)
    return {
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


def delete_los(request: Request, db: Session, *, path_id: str) -> str:
    los_path = ws_service.survey_los.get(db, path_id)
    survey_id = los_path.survey_id
    log_audit_event(
        db=db,
        request=request,
        action="los_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=_actor_id(request),
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
        },
    )
    ws_service.survey_los.delete(db, path_id)
    return f"/admin/network/site-survey/{survey_id}"
