"""Service helpers for admin network fiber plant web routes."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.fiber_change_request import FiberChangeRequestStatus
from app.services import fiber_change_requests as change_request_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services import web_network_fiber as web_network_fiber_service
from app.services.audit_helpers import build_audit_activities

logger = logging.getLogger(__name__)

_coerce_float_or_none = web_network_core_runtime_service.coerce_float_or_none


def form_optional_str(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) else None


def form_getlist_str(form: FormData, key: str) -> list[str]:
    return [value for value in form.getlist(key) if isinstance(value, str)]


def change_requests_page_data(
    db: Session,
    *,
    bulk_status: str | None,
    skipped: str | None,
) -> dict[str, object]:
    requests = change_request_service.list_requests(db, status=FiberChangeRequestStatus.pending)
    conflicts = {
        str(req.id): web_network_fiber_service.has_change_request_conflict(db, req)
        for req in requests
    }
    return {
        "requests": requests,
        "conflicts": conflicts,
        "bulk_status": bulk_status,
        "skipped": skipped,
    }


def change_request_detail_page_data(
    db: Session,
    *,
    request_id: str,
    error: str | None,
) -> dict[str, object]:
    change_request = change_request_service.get_request(db, request_id)
    asset_data: dict[str, object] = {}
    conflict = web_network_fiber_service.has_change_request_conflict(db, change_request)
    if change_request.asset_id:
        asset = web_network_core_devices_service.get_change_request_asset(
            db, change_request.asset_type, str(change_request.asset_id)
        )
        asset_data = web_network_fiber_service.serialize_asset(asset)

    return {
        "change_request": change_request,
        "asset_data": asset_data,
        "conflict": conflict,
        "pending": change_request.status == FiberChangeRequestStatus.pending,
        "error": error,
        "activities": build_audit_activities(db, "fiber_change_request", request_id, limit=10),
    }


def approve_change_request(
    db: Session,
    *,
    request_id: str,
    reviewer_person_id: str,
    review_notes: str | None,
    force_apply: bool,
) -> tuple[bool, str | None]:
    change_request = change_request_service.get_request(db, request_id)
    if web_network_fiber_service.has_change_request_conflict(db, change_request) and not force_apply:
        return False, "conflict"
    change_request_service.approve_request(
        db,
        request_id,
        reviewer_person_id=reviewer_person_id,
        review_notes=review_notes,
    )
    return True, None


def reject_change_request(
    db: Session,
    *,
    request_id: str,
    reviewer_person_id: str,
    review_notes: str | None,
) -> str | None:
    if not review_notes or not review_notes.strip():
        return "reject_note_required"
    change_request_service.reject_request(
        db,
        request_id,
        reviewer_person_id=reviewer_person_id,
        review_notes=review_notes,
    )
    return None


def bulk_approve_change_requests(
    db: Session,
    *,
    request_ids: list[str],
    reviewer_person_id: str,
    force_apply: bool,
) -> dict[str, object]:
    skipped = 0
    approved_request_ids: list[str] = []
    for request_id in request_ids:
        change_request = change_request_service.get_request(db, request_id)
        if web_network_fiber_service.has_change_request_conflict(db, change_request) and not force_apply:
            skipped += 1
            continue
        change_request_service.approve_request(
            db,
            request_id,
            reviewer_person_id=reviewer_person_id,
            review_notes="Bulk approved",
        )
        approved_request_ids.append(request_id)
    return {"skipped": skipped, "approved_request_ids": approved_request_ids}


def update_asset_position_data(db: Session, body: dict[str, object]) -> tuple[dict[str, object], int]:
    asset_type = body.get("type")
    asset_id = body.get("id")
    latitude_raw = body.get("latitude")
    longitude_raw = body.get("longitude")

    if not isinstance(asset_type, str) or not isinstance(asset_id, str):
        return {"error": "Missing required fields"}, 400
    if latitude_raw is None or longitude_raw is None:
        return {"error": "Missing required fields"}, 400

    latitude = _coerce_float_or_none(latitude_raw)
    longitude = _coerce_float_or_none(longitude_raw)
    if latitude is None or longitude is None:
        return {"error": "Invalid coordinates"}, 400

    try:
        payload, status_code = web_network_fiber_service.update_asset_position(
            db,
            asset_type=asset_type,
            asset_id=asset_id,
            latitude=latitude,
            longitude=longitude,
        )
        return payload, status_code
    except HTTPException as exc:
        db.rollback()
        return {"error": str(exc.detail)}, exc.status_code
    except Exception as exc:
        db.rollback()
        return {"error": str(exc)}, 500
