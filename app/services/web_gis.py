"""Service helpers for admin GIS web routes."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.gis import GeoAreaType, GeoLayerSource, GeoLayerType, GeoLocationType
from app.schemas.gis import (
    GeoAreaCreate,
    GeoAreaUpdate,
    GeoLayerCreate,
    GeoLayerUpdate,
    GeoLocationCreate,
    GeoLocationUpdate,
)
from app.services import gis as gis_service


def _enum_values(enum_cls) -> list[str]:
    return [item.value for item in enum_cls]


def _parse_json_object(raw: str | None, *, default: dict | None) -> dict | None:
    text = str(raw or "").strip()
    if not text:
        return default
    parsed = json.loads(text)
    if parsed is None:
        return default
    if not isinstance(parsed, dict):
        raise ValueError("JSON value must be an object")
    return parsed


def _normalize_layer_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def build_index_data(db: Session, *, tab: str) -> dict[str, Any]:
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

    map_markers = []
    for location in locations:
        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if lat is None or lon is None:
            continue
        location_type = (
            location.location_type.value
            if hasattr(location.location_type, "value")
            else str(location.location_type or "")
        )
        map_markers.append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "name": str(location.name or ""),
                "type": location_type,
                "id": str(location.id),
            }
        )

    area_features = []
    for area in areas:
        if not getattr(area, "geometry_geojson", None):
            continue
        area_type = (
            area.area_type.value
            if hasattr(area.area_type, "value")
            else str(area.area_type or "")
        )
        area_features.append(
            {
                "type": "Feature",
                "geometry": area.geometry_geojson,
                "properties": {
                    "id": str(area.id),
                    "name": str(area.name or ""),
                    "area_type": area_type,
                    "is_active": bool(area.is_active),
                },
            }
        )

    layer_overlays = [
        {
            "id": str(layer.id),
            "name": str(layer.name or ""),
            "layer_key": str(layer.layer_key or ""),
            "layer_type": layer.layer_type.value
            if hasattr(layer.layer_type, "value")
            else str(layer.layer_type or ""),
            "source_type": layer.source_type.value
            if hasattr(layer.source_type, "value")
            else str(layer.source_type or ""),
            "style": layer.style or {},
            "is_active": bool(layer.is_active),
        }
        for layer in layers
        if layer.is_active and layer.layer_key
    ]

    return {
        "active_tab": tab,
        "locations": locations,
        "areas": areas,
        "layers": layers,
        "coverage_areas": coverage_areas,
        "map_markers": map_markers,
        "area_features": area_features,
        "layer_overlays": layer_overlays,
    }


def location_form_data(
    *,
    location=None,
    action_url: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "location": location,
        "action_url": action_url,
        "error": error,
    }


def get_location(db: Session, *, location_id: str):
    return gis_service.geo_locations.get(db=db, location_id=location_id)


def delete_location(db: Session, *, location_id: str):
    return gis_service.geo_locations.delete(db=db, location_id=location_id)


def area_form_data(
    *,
    area=None,
    action_url: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "area": area,
        "action_url": action_url,
        "error": error,
        "area_types": _enum_values(GeoAreaType),
    }


def get_area(db: Session, *, area_id: str):
    return gis_service.geo_areas.get(db=db, area_id=area_id)


def delete_area(db: Session, *, area_id: str):
    return gis_service.geo_areas.delete(db=db, area_id=area_id)


def layer_form_data(
    *,
    layer=None,
    action_url: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "layer": layer,
        "action_url": action_url,
        "error": error,
        "layer_types": _enum_values(GeoLayerType),
        "source_types": _enum_values(GeoLayerSource),
    }


def get_layer(db: Session, *, layer_id: str):
    return gis_service.geo_layers.get(db=db, layer_id=layer_id)


def delete_layer(db: Session, *, layer_id: str):
    return gis_service.geo_layers.delete(db=db, layer_id=layer_id)


def create_location_from_form(
    db: Session,
    *,
    name: str,
    location_type: str,
    latitude: float,
    longitude: float,
    notes: str | None,
    is_active: str | None,
):
    payload = GeoLocationCreate(
        name=name,
        location_type=GeoLocationType(location_type),
        latitude=latitude,
        longitude=longitude,
        notes=notes or None,
        is_active=is_active == "true",
    )
    return gis_service.geo_locations.create(db=db, payload=payload)


def update_location_from_form(
    db: Session,
    *,
    location_id: str,
    name: str,
    location_type: str,
    latitude: float,
    longitude: float,
    notes: str | None,
    is_active: str | None,
):
    payload = GeoLocationUpdate(
        name=name,
        location_type=GeoLocationType(location_type),
        latitude=latitude,
        longitude=longitude,
        notes=notes or None,
        is_active=is_active == "true",
    )
    return gis_service.geo_locations.update(
        db=db,
        location_id=location_id,
        payload=payload,
    )


def create_area_from_form(
    db: Session,
    *,
    name: str,
    area_type: str,
    geometry_geojson: str,
    notes: str | None,
    is_active: str | None,
):
    payload = GeoAreaCreate(
        name=name,
        area_type=GeoAreaType(area_type),
        geometry_geojson=_parse_json_object(geometry_geojson, default=None),
        notes=notes or None,
        is_active=is_active == "true",
    )
    return gis_service.geo_areas.create(db=db, payload=payload)


def update_area_from_form(
    db: Session,
    *,
    area_id: str,
    name: str,
    area_type: str,
    geometry_geojson: str,
    notes: str | None,
    is_active: str | None,
):
    payload = GeoAreaUpdate(
        name=name,
        area_type=GeoAreaType(area_type),
        geometry_geojson=_parse_json_object(geometry_geojson, default=None),
        notes=notes or None,
        is_active=is_active == "true",
    )
    return gis_service.geo_areas.update(db=db, area_id=area_id, payload=payload)


def create_layer_from_form(
    db: Session,
    *,
    name: str,
    layer_key: str,
    layer_type: str,
    source_type: str,
    style: str,
    filters: str,
    is_active: str | None,
):
    payload = GeoLayerCreate(
        name=name,
        layer_key=_normalize_layer_key(layer_key),
        layer_type=GeoLayerType(layer_type),
        source_type=GeoLayerSource(source_type),
        style=_parse_json_object(style, default={}),
        filters=_parse_json_object(filters, default={}),
        is_active=is_active == "true",
    )
    return gis_service.geo_layers.create(db=db, payload=payload)


def update_layer_from_form(
    db: Session,
    *,
    layer_id: str,
    name: str,
    layer_key: str,
    layer_type: str,
    source_type: str,
    style: str,
    filters: str,
    is_active: str | None,
):
    payload = GeoLayerUpdate(
        name=name,
        layer_key=_normalize_layer_key(layer_key),
        layer_type=GeoLayerType(layer_type),
        source_type=GeoLayerSource(source_type),
        style=_parse_json_object(style, default={}),
        filters=_parse_json_object(filters, default={}),
        is_active=is_active == "true",
    )
    return gis_service.geo_layers.update(db=db, layer_id=layer_id, payload=payload)
