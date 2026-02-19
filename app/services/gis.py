from __future__ import annotations

import builtins

from fastapi import HTTPException
from geoalchemy2.functions import ST_Contains, ST_Distance, ST_DWithin, ST_MakePoint, ST_SetSRID
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.gis import (
    GeoArea,
    GeoAreaType,
    GeoLayer,
    GeoLayerSource,
    GeoLayerType,
    GeoLocation,
    GeoLocationType,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.services.crud import CRUDManager
from app.services.query_builders import apply_active_state, apply_optional_equals
from app.services.response import ListResponseMixin, list_response
from app.schemas.gis import (
    GeoAreaUpdate,
    GeoFeatureCollectionRead,
    GeoFeatureRead,
    GeoLayerUpdate,
    GeoLocationUpdate,
)



class GeoLocations(CRUDManager[GeoLocation]):
    model = GeoLocation
    not_found_detail = "Geo location not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        location_type: str | None,
        address_id: str | None,
        pop_site_id: str | None,
        is_active: bool | None,
        min_latitude: float | None,
        min_longitude: float | None,
        max_latitude: float | None,
        max_longitude: float | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(GeoLocation)
        if location_type:
            query = query.filter(
                GeoLocation.location_type
                == validate_enum(location_type, GeoLocationType, "location_type")
            )
        query = apply_optional_equals(
            query,
            {
                GeoLocation.address_id: address_id,
                GeoLocation.pop_site_id: pop_site_id,
            },
        )
        query = apply_active_state(query, GeoLocation.is_active, is_active)
        if None not in (min_latitude, min_longitude, max_latitude, max_longitude):
            query = query.filter(GeoLocation.latitude >= min_latitude)
            query = query.filter(GeoLocation.longitude >= min_longitude)
            query = query.filter(GeoLocation.latitude <= max_latitude)
            query = query.filter(GeoLocation.longitude <= max_longitude)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": GeoLocation.created_at, "name": GeoLocation.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, location_id: str):
        return super().get(db, location_id)

    @classmethod
    def update(cls, db: Session, location_id: str, payload: GeoLocationUpdate):
        return super().update(db, location_id, payload)

    @classmethod
    def delete(cls, db: Session, location_id: str):
        return super().delete(db, location_id)

    @staticmethod
    def find_nearby(
        db: Session,
        latitude: float,
        longitude: float,
        radius_meters: float,
        location_type: str | None = None,
        limit: int = 100,
    ) -> builtins.list[GeoLocation]:
        """Find locations within a radius using PostGIS spatial query.

        Args:
            db: Database session
            latitude: Center point latitude
            longitude: Center point longitude
            radius_meters: Search radius in meters
            location_type: Optional filter by location type
            limit: Maximum results to return

        Returns:
            List of GeoLocation objects ordered by distance
        """
        # Create point geometry for the search center
        point = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)

        # Build query with spatial filter
        query = (
            db.query(GeoLocation)
            .filter(GeoLocation.is_active.is_(True))
            .filter(GeoLocation.geom.isnot(None))
            .filter(
                ST_DWithin(
                    func.ST_Transform(GeoLocation.geom, 3857),
                    func.ST_Transform(point, 3857),
                    radius_meters
                )
            )
            .order_by(ST_Distance(GeoLocation.geom, point))
        )

        if location_type:
            query = query.filter(
                GeoLocation.location_type
                == validate_enum(location_type, GeoLocationType, "location_type")
            )

        return query.limit(limit).all()

    @staticmethod
    def find_in_area(
        db: Session,
        area_id: str,
        location_type: str | None = None,
        limit: int = 100,
    ) -> builtins.list[GeoLocation]:
        """Find locations within a GeoArea polygon.

        Args:
            db: Database session
            area_id: GeoArea ID to search within
            location_type: Optional filter by location type
            limit: Maximum results to return

        Returns:
            List of GeoLocation objects within the area
        """
        area = db.get(GeoArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Geo area not found")

        if not area.geom:
            raise HTTPException(status_code=400, detail="Area has no geometry")

        query = (
            db.query(GeoLocation)
            .filter(GeoLocation.is_active.is_(True))
            .filter(GeoLocation.geom.isnot(None))
            .filter(ST_Contains(area.geom, GeoLocation.geom))
        )

        if location_type:
            query = query.filter(
                GeoLocation.location_type
                == validate_enum(location_type, GeoLocationType, "location_type")
            )

        return query.limit(limit).all()


class GeoAreas(CRUDManager[GeoArea]):
    model = GeoArea
    not_found_detail = "Geo area not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        area_type: str | None,
        is_active: bool | None,
        min_latitude: float | None,
        min_longitude: float | None,
        max_latitude: float | None,
        max_longitude: float | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(GeoArea)
        if area_type:
            query = query.filter(
                GeoArea.area_type == validate_enum(area_type, GeoAreaType, "area_type")
            )
        query = apply_active_state(query, GeoArea.is_active, is_active)
        if None not in (min_latitude, min_longitude, max_latitude, max_longitude):
            query = query.filter(GeoArea.max_latitude >= min_latitude)
            query = query.filter(GeoArea.max_longitude >= min_longitude)
            query = query.filter(GeoArea.min_latitude <= max_latitude)
            query = query.filter(GeoArea.min_longitude <= max_longitude)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": GeoArea.created_at, "name": GeoArea.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, area_id: str):
        return super().get(db, area_id)

    @classmethod
    def update(cls, db: Session, area_id: str, payload: GeoAreaUpdate):
        return super().update(db, area_id, payload)

    @classmethod
    def delete(cls, db: Session, area_id: str):
        return super().delete(db, area_id)

    @staticmethod
    def contains_point(
        db: Session,
        area_id: str,
        latitude: float,
        longitude: float,
    ) -> bool:
        """Check if a point is contained within a GeoArea.

        Args:
            db: Database session
            area_id: GeoArea ID to check
            latitude: Point latitude
            longitude: Point longitude

        Returns:
            True if point is within the area, False otherwise
        """
        area = db.get(GeoArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Geo area not found")

        if not area.geom:
            return False

        point = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)
        result = db.query(
            ST_Contains(area.geom, point)
        ).scalar()
        return bool(result)

    @staticmethod
    def find_containing(
        db: Session,
        latitude: float,
        longitude: float,
        area_type: str | None = None,
    ) -> builtins.list[GeoArea]:
        """Find all areas that contain a given point.

        Args:
            db: Database session
            latitude: Point latitude
            longitude: Point longitude
            area_type: Optional filter by area type

        Returns:
            List of GeoArea objects containing the point
        """
        point = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)

        query = (
            db.query(GeoArea)
            .filter(GeoArea.is_active.is_(True))
            .filter(GeoArea.geom.isnot(None))
            .filter(ST_Contains(GeoArea.geom, point))
        )

        if area_type:
            query = query.filter(
                GeoArea.area_type == validate_enum(area_type, GeoAreaType, "area_type")
            )

        return query.all()


class GeoLayers(CRUDManager[GeoLayer]):
    model = GeoLayer
    not_found_detail = "Geo layer not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @classmethod
    def get(cls, db: Session, layer_id: str):
        return super().get(db, layer_id)

    @staticmethod
    def get_by_key(db: Session, layer_key: str):
        layer = db.query(GeoLayer).filter(GeoLayer.layer_key == layer_key).first()
        if not layer:
            raise HTTPException(status_code=404, detail="Geo layer not found")
        return layer

    @staticmethod
    def list(
        db: Session,
        layer_type: str | None,
        source_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(GeoLayer)
        if layer_type:
            query = query.filter(
                GeoLayer.layer_type
                == validate_enum(layer_type, GeoLayerType, "layer_type")
            )
        if source_type:
            query = query.filter(
                GeoLayer.source_type
                == validate_enum(source_type, GeoLayerSource, "source_type")
            )
        query = apply_active_state(query, GeoLayer.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": GeoLayer.created_at, "name": GeoLayer.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def update(cls, db: Session, layer_id: str, payload: GeoLayerUpdate):
        return super().update(db, layer_id, payload)

    @classmethod
    def delete(cls, db: Session, layer_id: str):
        return super().delete(db, layer_id)


class GeoFeatures(ListResponseMixin):
    @staticmethod
    def list_features(
        db: Session,
        layer_key: str,
        min_latitude: float | None,
        min_longitude: float | None,
        max_latitude: float | None,
        max_longitude: float | None,
        limit: int,
        offset: int,
    ) -> list[GeoFeatureRead]:
        return _build_layer_features(
            db,
            layer_key,
            min_latitude,
            min_longitude,
            max_latitude,
            max_longitude,
            limit,
            offset,
        )

    @staticmethod
    def list_features_response(
        db: Session,
        layer_key: str,
        min_latitude: float | None,
        min_longitude: float | None,
        max_latitude: float | None,
        max_longitude: float | None,
        limit: int,
        offset: int,
    ):
        items = GeoFeatures.list_features(
            db,
            layer_key,
            min_latitude,
            min_longitude,
            max_latitude,
            max_longitude,
            limit,
            offset,
        )
        return list_response(items, limit, offset)

    @staticmethod
    def feature_collection(
        db: Session,
        layer_key: str,
        min_latitude: float | None,
        min_longitude: float | None,
        max_latitude: float | None,
        max_longitude: float | None,
        limit: int,
        offset: int,
    ) -> GeoFeatureCollectionRead:
        features = GeoFeatures.list_features(
            db,
            layer_key,
            min_latitude,
            min_longitude,
            max_latitude,
            max_longitude,
            limit,
            offset,
        )
        return GeoFeatureCollectionRead(features=features)


def _build_layer_features(
    db: Session,
    layer_key: str,
    min_latitude: float | None,
    min_longitude: float | None,
    max_latitude: float | None,
    max_longitude: float | None,
    limit: int,
    offset: int,
) -> list[GeoFeatureRead]:
    layer = GeoLayers.get_by_key(db, layer_key)
    features: list[GeoFeatureRead] = []
    if layer.source_type.value == "locations":
        locations = GeoLocations.list(
            db,
            location_type=None,
            address_id=None,
            pop_site_id=None,
            is_active=True,
            min_latitude=min_latitude,
            min_longitude=min_longitude,
            max_latitude=max_latitude,
            max_longitude=max_longitude,
            order_by="created_at",
            order_dir="desc",
            limit=limit,
            offset=offset,
        )
        for location in locations:
            features.append(
                GeoFeatureRead(
                    id=str(location.id),
                    geometry={
                        "type": "Point",
                        "coordinates": [location.longitude, location.latitude],
                    },
                    properties={
                        "name": location.name,
                        "location_type": location.location_type.value,
                        "address_id": str(location.address_id)
                        if location.address_id
                        else None,
                        "pop_site_id": str(location.pop_site_id)
                        if location.pop_site_id
                        else None,
                        "tags": location.tags,
                        "metadata": location.metadata_,
                    },
                )
            )
    elif layer.source_type.value == "areas":
        areas = GeoAreas.list(
            db,
            area_type=None,
            is_active=True,
            min_latitude=min_latitude,
            min_longitude=min_longitude,
            max_latitude=max_latitude,
            max_longitude=max_longitude,
            order_by="created_at",
            order_dir="desc",
            limit=limit,
            offset=offset,
        )
        for area in areas:
            features.append(
                GeoFeatureRead(
                    id=str(area.id),
                    geometry=area.geometry_geojson,
                    properties={
                        "name": area.name,
                        "area_type": area.area_type.value,
                        "tags": area.tags,
                        "metadata": area.metadata_,
                    },
                )
            )
    return features


geo_locations = GeoLocations()
geo_areas = GeoAreas()
geo_layers = GeoLayers()
geo_features = GeoFeatures()
