import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.network import (
    FdhCabinet,
    FiberSegment,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberTerminationPoint,
    Splitter,
    SplitterPort,
)

ASSET_MODEL_MAP = {
    "fdh_cabinet": FdhCabinet,
    "splice_closure": FiberSpliceClosure,
    "fiber_segment": FiberSegment,
    "fiber_splice": FiberSplice,
    "fiber_splice_tray": FiberSpliceTray,
    "fiber_strand": FiberStrand,
    "fiber_termination_point": FiberTerminationPoint,
    "splitter": Splitter,
    "splitter_port": SplitterPort,
}


def _normalize_asset_type(asset_type: str) -> str:
    normalized = (asset_type or "").strip().lower()
    if normalized == "fiber_splice_closure":
        return "splice_closure"
    return normalized


def _geojson_to_geom(geojson: dict) -> object:
    geojson_str = json.dumps(geojson)
    return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_str), 4326)


def _prepare_payload(model, payload: dict) -> dict:
    data = dict(payload or {})
    geojson_value = data.pop("geojson", None)
    if geojson_value:
        if hasattr(model, "route_geom"):
            data["route_geom"] = geojson_value
        elif hasattr(model, "geom"):
            data["geom"] = geojson_value
    for key in ("route_geom", "geom"):
        if key in data and isinstance(data[key], dict):
            data[key] = _geojson_to_geom(data[key])
    return data


def _get_model(asset_type: str):
    normalized = _normalize_asset_type(asset_type)
    model = ASSET_MODEL_MAP.get(normalized)
    if not model:
        raise HTTPException(status_code=400, detail="Unsupported asset type")
    return normalized, model


def create_request(
    db: Session,
    asset_type: str,
    asset_id: str | None,
    operation: FiberChangeRequestOperation,
    payload: dict,
    requested_by_person_id: str | None,
    requested_by_vendor_id: str | None,
):
    normalized, _ = _get_model(asset_type)
    request = FiberChangeRequest(
        asset_type=normalized,
        asset_id=asset_id,
        operation=operation,
        payload=payload,
        status=FiberChangeRequestStatus.pending,
        requested_by_person_id=requested_by_person_id,
        requested_by_vendor_id=requested_by_vendor_id,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def list_requests(db: Session, status: FiberChangeRequestStatus | None = None):
    query = db.query(FiberChangeRequest)
    if status:
        query = query.filter(FiberChangeRequest.status == status)
    return query.order_by(FiberChangeRequest.created_at.desc()).all()


def get_request(db: Session, request_id: str) -> FiberChangeRequest:
    request = db.get(FiberChangeRequest, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Change request not found")
    return request


def reject_request(db: Session, request_id: str, reviewer_person_id: str, review_notes: str | None):
    request = get_request(db, request_id)
    if request.status != FiberChangeRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Change request already processed")
    request.status = FiberChangeRequestStatus.rejected
    request.reviewed_by_person_id = reviewer_person_id
    request.review_notes = review_notes
    request.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(request)
    return request


def _apply_request(db: Session, request: FiberChangeRequest):
    _, model = _get_model(request.asset_type)
    payload = _prepare_payload(model, request.payload)
    if request.operation == FiberChangeRequestOperation.create:
        asset = model(**payload)
        db.add(asset)
        db.flush()
        request.asset_id = asset.id
    elif request.operation == FiberChangeRequestOperation.update:
        if not request.asset_id:
            raise HTTPException(status_code=400, detail="Missing asset_id for update")
        asset = db.get(model, request.asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        for key, value in payload.items():
            if key in {"id", "created_at", "updated_at"}:
                continue
            if hasattr(asset, key):
                setattr(asset, key, value)
    elif request.operation == FiberChangeRequestOperation.delete:
        if not request.asset_id:
            raise HTTPException(status_code=400, detail="Missing asset_id for delete")
        asset = db.get(model, request.asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        if hasattr(asset, "is_active"):
            setattr(asset, "is_active", False)
        else:
            db.delete(asset)
    else:
        raise HTTPException(status_code=400, detail="Invalid operation")


def approve_request(db: Session, request_id: str, reviewer_person_id: str, review_notes: str | None):
    request = get_request(db, request_id)
    if request.status != FiberChangeRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Change request already processed")
    _apply_request(db, request)
    request.status = FiberChangeRequestStatus.applied
    request.reviewed_by_person_id = reviewer_person_id
    request.review_notes = review_notes
    request.reviewed_at = datetime.now(timezone.utc)
    request.applied_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(request)
    return request
