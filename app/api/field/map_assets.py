from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import FieldMapAsset
from app.services.auth_dependencies import require_user_auth
from app.services.field.map_assets import field_map_assets

router = APIRouter(tags=["field-map-assets"])


@router.get("/map-assets", response_model=ListResponse[FieldMapAsset])
def list_field_map_assets(
    asset_type: list[str] | None = Query(default=None),
    updated_since: datetime | None = None,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    _auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_map_assets.list(
        db,
        asset_types=asset_type,
        updated_since=updated_since,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/map-assets/nearby", response_model=list[FieldMapAsset])
def list_nearby_field_map_assets(
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
    radius_m: float = Query(default=500.0, gt=0, le=10_000),
    asset_type: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    _auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_map_assets.nearby(
        db,
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        asset_types=asset_type,
        limit=limit,
    )
