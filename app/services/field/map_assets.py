"""Read-only field map assets backed by sub's native plant/GIS tables."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any, Protocol, cast

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.gis import ServiceBuilding
from app.models.network import FdhCabinet, FiberAccessPoint, FiberSpliceClosure
from app.models.wireless_mast import WirelessMast

_EARTH_RADIUS_M = 6_371_000.0
_METERS_PER_DEGREE = 111_320.0
AssetPayload = dict[str, Any]
AssetPayloads = list[AssetPayload]
AssetTypeFilter = list[str] | None


class _MapAssetModel(Protocol):
    is_active: Any
    latitude: Any
    longitude: Any
    updated_at: Any


@dataclass(frozen=True)
class _AssetConfig:
    asset_type: str
    model: type[_MapAssetModel]
    title: str
    subtitle: Callable[[Any], str | None]


def _compact(parts: list[str | None]) -> str | None:
    text = " - ".join(part for part in parts if part)
    return text or None


def _label(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _status(row: Any) -> str | None:
    status = _label(getattr(row, "status", None))
    if status:
        return status
    active = getattr(row, "is_active", None)
    if active is None:
        return None
    return "active" if active else "inactive"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    r_lat1 = radians(lat1)
    r_lat2 = radians(lat2)
    a = sin(d_lat / 2) ** 2 + cos(r_lat1) * cos(r_lat2) * sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


def _bounds(
    latitude: float, longitude: float, radius_m: float
) -> tuple[float, float, float, float]:
    lat_delta = radius_m / _METERS_PER_DEGREE
    lng_scale = max(cos(radians(latitude)), 0.01)
    lng_delta = radius_m / (_METERS_PER_DEGREE * lng_scale)
    return (
        latitude - lat_delta,
        latitude + lat_delta,
        longitude - lng_delta,
        longitude + lng_delta,
    )


_ASSETS: dict[str, _AssetConfig] = {
    "fdh_cabinet": _AssetConfig(
        asset_type="fdh_cabinet",
        model=cast(type[_MapAssetModel], FdhCabinet),
        title="name",
        subtitle=lambda row: row.code,
    ),
    "splice_closure": _AssetConfig(
        asset_type="splice_closure",
        model=cast(type[_MapAssetModel], FiberSpliceClosure),
        title="name",
        subtitle=lambda row: None,
    ),
    "fiber_access_point": _AssetConfig(
        asset_type="fiber_access_point",
        model=cast(type[_MapAssetModel], FiberAccessPoint),
        title="name",
        subtitle=lambda row: _compact(
            [row.code, row.access_point_type, row.placement, row.street]
        ),
    ),
    "service_building": _AssetConfig(
        asset_type="service_building",
        model=cast(type[_MapAssetModel], ServiceBuilding),
        title="name",
        subtitle=lambda row: _compact([row.code, row.clli, row.street]),
    ),
    "wireless_mast": _AssetConfig(
        asset_type="wireless_mast",
        model=cast(type[_MapAssetModel], WirelessMast),
        title="name",
        subtitle=lambda row: _compact([row.structure_type, row.owner]),
    ),
}


def _configs(asset_types: AssetTypeFilter) -> list[_AssetConfig]:
    if not asset_types:
        return list(_ASSETS.values())
    unknown = sorted(set(asset_types) - set(_ASSETS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported map asset type: {', '.join(unknown)}",
        )
    return [_ASSETS[asset_type] for asset_type in asset_types]


def _base_query(db: Session, config: _AssetConfig):
    model = config.model
    return (
        db.query(model)
        .filter(model.is_active.is_(True))
        .filter(model.latitude.isnot(None))
        .filter(model.longitude.isnot(None))
    )


def _serialize(
    row: Any,
    config: _AssetConfig,
    *,
    distance_m: float | None = None,
) -> AssetPayload:
    return {
        "id": row.id,
        "type": config.asset_type,
        "title": getattr(row, config.title),
        "subtitle": config.subtitle(row),
        "latitude": float(row.latitude),
        "longitude": float(row.longitude),
        "status": _status(row),
        "updated_at": getattr(row, "updated_at", None),
        "distance_m": distance_m,
    }


class FieldMapAssets:
    @staticmethod
    def list(
        db: Session,
        *,
        asset_types: AssetTypeFilter = None,
        updated_since: datetime | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> AssetPayloads:
        items: AssetPayloads = []
        for config in _configs(asset_types):
            model = config.model
            query = _base_query(db, config)
            if updated_since is not None:
                query = query.filter(model.updated_at >= updated_since)
            rows = query.all()
            items.extend(_serialize(row, config) for row in rows)

        items.sort(key=lambda item: (item["type"], item["title"], str(item["id"])))
        return items[offset : offset + limit]

    @staticmethod
    def nearby(
        db: Session,
        *,
        latitude: float,
        longitude: float,
        radius_m: float = 500.0,
        asset_types: AssetTypeFilter = None,
        limit: int = 50,
    ) -> AssetPayloads:
        min_lat, max_lat, min_lng, max_lng = _bounds(latitude, longitude, radius_m)
        items: AssetPayloads = []
        for config in _configs(asset_types):
            model = config.model
            rows = (
                _base_query(db, config)
                .filter(model.latitude.between(min_lat, max_lat))
                .filter(model.longitude.between(min_lng, max_lng))
                .all()
            )
            for row in rows:
                distance = _haversine_m(
                    latitude,
                    longitude,
                    float(row.latitude),
                    float(row.longitude),
                )
                if distance <= radius_m:
                    items.append(_serialize(row, config, distance_m=round(distance, 1)))

        items.sort(key=lambda item: (item["distance_m"] or 0.0, item["title"]))
        return items[:limit]

    @staticmethod
    def search(
        db: Session,
        query: str,
        *,
        asset_types: AssetTypeFilter = None,
        limit: int = 20,
    ) -> AssetPayloads:
        term = query.strip().casefold()
        if not term:
            return []

        items: AssetPayloads = []
        for config in _configs(asset_types):
            for row in _base_query(db, config).all():
                payload = _serialize(row, config)
                searchable = " ".join(
                    part
                    for part in [
                        payload["title"],
                        payload["subtitle"],
                        payload["status"],
                    ]
                    if part
                ).casefold()
                if term in searchable:
                    items.append(payload)
                    if len(items) >= limit:
                        return items

        items.sort(key=lambda item: (item["type"], item["title"]))
        return items[:limit]


field_map_assets = FieldMapAssets()
