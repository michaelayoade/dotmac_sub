"""Service helpers for admin GIS web routes."""

from __future__ import annotations

from app.models.gis import GeoAreaType, GeoLocationType
from app.schemas.gis import GeoLocationCreate, GeoLocationUpdate
from app.services import gis as gis_service


def list_page_data(db, *, tab: str) -> dict[str, object]:
    locations = gis_service.geo_locations.list(
        db=db,
        location_type=None,
        address_id=None,
        pop_site_id=None,
        is_active=None,
        min_latitude=None,
        min_longitude=None,
        max_latitude=None,
        max_longitude=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    areas = gis_service.geo_areas.list(
        db=db,
        area_type=None,
        is_active=None,
        min_latitude=None,
        min_longitude=None,
        max_latitude=None,
        max_longitude=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    layers = gis_service.geo_layers.list(
        db=db,
        layer_type=None,
        source_type=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )

    coverage_areas = sum(
        1
        for area in areas
        if area.area_type in {GeoAreaType.coverage, GeoAreaType.service_area}
    )

    return {
        "active_tab": tab,
        "locations": locations,
        "areas": areas,
        "layers": layers,
        "coverage_areas": coverage_areas,
    }


def build_location_form_context(
    *,
    location,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "location": location,
        "action_url": action_url,
        "error": error,
    }
    return context


def build_location_create_payload(
    *,
    name: str,
    location_type: str,
    latitude: float,
    longitude: float,
    notes: str | None,
    is_active: str | None,
) -> GeoLocationCreate:
    return GeoLocationCreate(
        name=name,
        location_type=GeoLocationType(location_type),
        latitude=latitude,
        longitude=longitude,
        notes=notes or None,
        is_active=is_active == "true",
    )


def build_location_update_payload(
    *,
    name: str,
    location_type: str,
    latitude: float,
    longitude: float,
    notes: str | None,
    is_active: str | None,
) -> GeoLocationUpdate:
    return GeoLocationUpdate(
        name=name,
        location_type=GeoLocationType(location_type),
        latitude=latitude,
        longitude=longitude,
        notes=notes or None,
        is_active=is_active == "true",
    )
