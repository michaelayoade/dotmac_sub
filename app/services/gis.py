from __future__ import annotations

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
from app.services.response import ListResponseMixin, list_response
from app.schemas.gis import (
    GeoAreaCreate,
    GeoAreaUpdate,
    GeoFeatureCollectionRead,
    GeoFeatureRead,
    GeoLayerCreate,
    GeoLayerUpdate,
    GeoLocationCreate,
    GeoLocationUpdate,
)



class GeoLocations(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: GeoLocationCreate):
        location = GeoLocation(**payload.model_dump())
        db.add(location)
        db.commit()
        db.refresh(location)
        return location

    @staticmethod
    def get(db: Session, location_id: str):
        location = db.get(GeoLocation, location_id)
        if not location:
            raise HTTPException(status_code=404, detail="Geo location not found")
        return location

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
        if address_id:
            query = query.filter(GeoLocation.address_id == address_id)
        if pop_site_id:
            query = query.filter(GeoLocation.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.filter(GeoLocation.is_active.is_(True))
        else:
            query = query.filter(GeoLocation.is_active == is_active)
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

    @staticmethod
    def update(db: Session, location_id: str, payload: GeoLocationUpdate):
        location = db.get(GeoLocation, location_id)
        if not location:
            raise HTTPException(status_code=404, detail="Geo location not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(location, key, value)
        db.commit()
        db.refresh(location)
        return location

    @staticmethod
    def delete(db: Session, location_id: str):
        location = db.get(GeoLocation, location_id)
        if not location:
            raise HTTPException(status_code=404, detail="Geo location not found")
        location.is_active = False
        db.commit()

    @staticmethod
    def find_nearby(
        db: Session,
        latitude: float,
        longitude: float,
        radius_meters: float,
        location_type: str | None = None,
        limit: int = 100,
    ) -> list[GeoLocation]:
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
    ) -> list[GeoLocation]:
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


class GeoAreas(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: GeoAreaCreate):
        area = GeoArea(**payload.model_dump())
        db.add(area)
        db.commit()
        db.refresh(area)
        return area

    @staticmethod
    def get(db: Session, area_id: str):
        area = db.get(GeoArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Geo area not found")
        return area

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
        if is_active is None:
            query = query.filter(GeoArea.is_active.is_(True))
        else:
            query = query.filter(GeoArea.is_active == is_active)
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

    @staticmethod
    def update(db: Session, area_id: str, payload: GeoAreaUpdate):
        area = db.get(GeoArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Geo area not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(area, key, value)
        db.commit()
        db.refresh(area)
        return area

    @staticmethod
    def delete(db: Session, area_id: str):
        area = db.get(GeoArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Geo area not found")
        area.is_active = False
        db.commit()

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
    ) -> list[GeoArea]:
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


class GeoLayers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: GeoLayerCreate):
        layer = GeoLayer(**payload.model_dump())
        db.add(layer)
        db.commit()
        db.refresh(layer)
        return layer

    @staticmethod
    def get(db: Session, layer_id: str):
        layer = db.get(GeoLayer, layer_id)
        if not layer:
            raise HTTPException(status_code=404, detail="Geo layer not found")
        return layer

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
        if is_active is None:
            query = query.filter(GeoLayer.is_active.is_(True))
        else:
            query = query.filter(GeoLayer.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": GeoLayer.created_at, "name": GeoLayer.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, layer_id: str, payload: GeoLayerUpdate):
        layer = db.get(GeoLayer, layer_id)
        if not layer:
            raise HTTPException(status_code=404, detail="Geo layer not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(layer, key, value)
        db.commit()
        db.refresh(layer)
        return layer

    @staticmethod
    def delete(db: Session, layer_id: str):
        layer = db.get(GeoLayer, layer_id)
        if not layer:
            raise HTTPException(status_code=404, detail="Geo layer not found")
        layer.is_active = False
        db.commit()


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
