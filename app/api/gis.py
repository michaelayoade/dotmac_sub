from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.gis import (
    ElevationRead,
    GeoAreaCreate,
    GeoAreaRead,
    GeoAreaUpdate,
    GeoFeatureCollectionRead,
    GeoFeatureRead,
    GeoLayerCreate,
    GeoLayerRead,
    GeoLayerUpdate,
    GeoLocationCreate,
    GeoLocationRead,
    GeoLocationUpdate,
)
from app.services import dem as dem_service
from app.services import gis as gis_service
from app.services import gis_sync as gis_sync_service

router = APIRouter(prefix="/gis")


@router.post(
    "/locations",
    response_model=GeoLocationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["gis-locations"],
)
def create_geo_location(payload: GeoLocationCreate, db: Session = Depends(get_db)):
    return gis_service.geo_locations.create(db, payload)


@router.get(
    "/locations/{location_id}",
    response_model=GeoLocationRead,
    tags=["gis-locations"],
)
def get_geo_location(location_id: str, db: Session = Depends(get_db)):
    return gis_service.geo_locations.get(db, location_id)


@router.get(
    "/locations",
    response_model=ListResponse[GeoLocationRead],
    tags=["gis-locations"],
)
def list_geo_locations(
    location_type: str | None = None,
    address_id: str | None = None,
    pop_site_id: str | None = None,
    is_active: bool | None = None,
    min_latitude: float | None = None,
    min_longitude: float | None = None,
    max_latitude: float | None = None,
    max_longitude: float | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return gis_service.geo_locations.list_response(
        db,
        location_type,
        address_id,
        pop_site_id,
        is_active,
        min_latitude,
        min_longitude,
        max_latitude,
        max_longitude,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/locations/{location_id}",
    response_model=GeoLocationRead,
    tags=["gis-locations"],
)
def update_geo_location(
    location_id: str, payload: GeoLocationUpdate, db: Session = Depends(get_db)
):
    return gis_service.geo_locations.update(db, location_id, payload)


@router.delete(
    "/locations/{location_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["gis-locations"],
)
def delete_geo_location(location_id: str, db: Session = Depends(get_db)):
    gis_service.geo_locations.delete(db, location_id)


@router.get(
    "/locations/nearby",
    response_model=list[GeoLocationRead],
    tags=["gis-spatial"],
)
def find_nearby_locations(
    latitude: float = Query(..., ge=-90, le=90, description="Center point latitude"),
    longitude: float = Query(..., ge=-180, le=180, description="Center point longitude"),
    radius: float = Query(..., gt=0, le=100000, description="Search radius in meters"),
    location_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Find locations within a radius of a point using PostGIS spatial query."""
    return gis_service.geo_locations.find_nearby(
        db, latitude, longitude, radius, location_type, limit
    )


@router.get(
    "/locations/in-area/{area_id}",
    response_model=list[GeoLocationRead],
    tags=["gis-spatial"],
)
def find_locations_in_area(
    area_id: str,
    location_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Find all locations within a GeoArea polygon."""
    return gis_service.geo_locations.find_in_area(db, area_id, location_type, limit)


@router.post(
    "/areas",
    response_model=GeoAreaRead,
    status_code=status.HTTP_201_CREATED,
    tags=["gis-areas"],
)
def create_geo_area(payload: GeoAreaCreate, db: Session = Depends(get_db)):
    return gis_service.geo_areas.create(db, payload)


@router.get(
    "/areas/{area_id}",
    response_model=GeoAreaRead,
    tags=["gis-areas"],
)
def get_geo_area(area_id: str, db: Session = Depends(get_db)):
    return gis_service.geo_areas.get(db, area_id)


@router.get(
    "/areas",
    response_model=ListResponse[GeoAreaRead],
    tags=["gis-areas"],
)
def list_geo_areas(
    area_type: str | None = None,
    is_active: bool | None = None,
    min_latitude: float | None = None,
    min_longitude: float | None = None,
    max_latitude: float | None = None,
    max_longitude: float | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return gis_service.geo_areas.list_response(
        db,
        area_type,
        is_active,
        min_latitude,
        min_longitude,
        max_latitude,
        max_longitude,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/areas/{area_id}",
    response_model=GeoAreaRead,
    tags=["gis-areas"],
)
def update_geo_area(area_id: str, payload: GeoAreaUpdate, db: Session = Depends(get_db)):
    return gis_service.geo_areas.update(db, area_id, payload)


@router.delete(
    "/areas/{area_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["gis-areas"],
)
def delete_geo_area(area_id: str, db: Session = Depends(get_db)):
    gis_service.geo_areas.delete(db, area_id)


@router.get(
    "/areas/{area_id}/contains-point",
    response_model=dict,
    tags=["gis-spatial"],
)
def check_area_contains_point(
    area_id: str,
    latitude: float = Query(..., ge=-90, le=90, description="Point latitude"),
    longitude: float = Query(..., ge=-180, le=180, description="Point longitude"),
    db: Session = Depends(get_db),
):
    """Check if a point is contained within a GeoArea polygon."""
    result = gis_service.geo_areas.contains_point(db, area_id, latitude, longitude)
    return {"area_id": area_id, "latitude": latitude, "longitude": longitude, "contained": result}


@router.get(
    "/areas/containing-point",
    response_model=list[GeoAreaRead],
    tags=["gis-spatial"],
)
def find_areas_containing_point(
    latitude: float = Query(..., ge=-90, le=90, description="Point latitude"),
    longitude: float = Query(..., ge=-180, le=180, description="Point longitude"),
    area_type: str | None = None,
    db: Session = Depends(get_db),
):
    """Find all areas that contain a given point."""
    return gis_service.geo_areas.find_containing(db, latitude, longitude, area_type)


@router.get(
    "/elevation",
    response_model=ElevationRead,
    tags=["gis-elevation"],
)
def get_elevation(
    latitude: float = Query(..., ge=-90, le=90, description="Point latitude"),
    longitude: float = Query(..., ge=-180, le=180, description="Point longitude"),
):
    return dem_service.get_elevation(latitude, longitude)


@router.post(
    "/layers",
    response_model=GeoLayerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["gis-layers"],
)
def create_geo_layer(payload: GeoLayerCreate, db: Session = Depends(get_db)):
    return gis_service.geo_layers.create(db, payload)


@router.get(
    "/layers/{layer_id}",
    response_model=GeoLayerRead,
    tags=["gis-layers"],
)
def get_geo_layer(layer_id: str, db: Session = Depends(get_db)):
    return gis_service.geo_layers.get(db, layer_id)


@router.get(
    "/layers",
    response_model=ListResponse[GeoLayerRead],
    tags=["gis-layers"],
)
def list_geo_layers(
    layer_type: str | None = None,
    source_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return gis_service.geo_layers.list_response(
        db,
        layer_type,
        source_type,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/layers/{layer_id}",
    response_model=GeoLayerRead,
    tags=["gis-layers"],
)
def update_geo_layer(layer_id: str, payload: GeoLayerUpdate, db: Session = Depends(get_db)):
    return gis_service.geo_layers.update(db, layer_id, payload)


@router.delete(
    "/layers/{layer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["gis-layers"],
)
def delete_geo_layer(layer_id: str, db: Session = Depends(get_db)):
    gis_service.geo_layers.delete(db, layer_id)


@router.get(
    "/layers/{layer_key}/features",
    response_model=ListResponse[GeoFeatureRead],
    tags=["gis-features"],
)
def list_layer_features(
    layer_key: str,
    min_latitude: float | None = None,
    min_longitude: float | None = None,
    max_latitude: float | None = None,
    max_longitude: float | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return gis_service.geo_features.list_features_response(
        db,
        layer_key,
        min_latitude,
        min_longitude,
        max_latitude,
        max_longitude,
        limit,
        offset,
    )


@router.get(
    "/layers/{layer_key}/feature-collection",
    response_model=GeoFeatureCollectionRead,
    tags=["gis-features"],
)
def get_layer_feature_collection(
    layer_key: str,
    min_latitude: float | None = None,
    min_longitude: float | None = None,
    max_latitude: float | None = None,
    max_longitude: float | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return gis_service.geo_features.feature_collection(
        db,
        layer_key,
        min_latitude,
        min_longitude,
        max_latitude,
        max_longitude,
        limit,
        offset,
    )


@router.post(
    "/sync",
    status_code=status.HTTP_200_OK,
    tags=["gis-sync"],
)
def sync_gis_sources(
    background_tasks: BackgroundTasks,
    sync_pops: bool = Query(default=True),
    sync_addresses: bool = Query(default=True),
    deactivate_missing: bool = Query(default=False),
    background: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    return gis_sync_service.geo_sync.sync_sources(
        db,
        background_tasks,
        sync_pops,
        sync_addresses,
        deactivate_missing,
        background,
    )
