"""Tests for GIS service."""

from app.models.gis import GeoLocationType, GeoLayerType, GeoAreaType
from app.schemas.gis import (
    GeoLocationCreate, GeoLocationUpdate,
    GeoLayerCreate, GeoLayerUpdate,
    GeoAreaCreate, GeoAreaUpdate,
)
from app.services import gis as gis_service


def test_create_geo_location(db_session):
    """Test creating a geo location."""
    location = gis_service.geo_locations.create(
        db_session,
        GeoLocationCreate(
            name="Main Office",
            location_type=GeoLocationType.address,
            latitude=40.7128,
            longitude=-74.0060,
        ),
    )
    assert location.name == "Main Office"
    assert location.location_type == GeoLocationType.address


def test_list_geo_locations_by_type(db_session):
    """Test listing geo locations filtered by type."""
    gis_service.geo_locations.create(
        db_session,
        GeoLocationCreate(
            name="Address 1",
            location_type=GeoLocationType.address,
            latitude=41.0,
            longitude=-73.0,
        ),
    )
    gis_service.geo_locations.create(
        db_session,
        GeoLocationCreate(
            name="Site 1",
            location_type=GeoLocationType.site,
            latitude=42.0,
            longitude=-72.0,
        ),
    )

    addresses = gis_service.geo_locations.list(
        db_session,
        location_type="address",
        address_id=None,
        pop_site_id=None,
        is_active=None,
        min_latitude=None,
        min_longitude=None,
        max_latitude=None,
        max_longitude=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert all(loc.location_type == GeoLocationType.address for loc in addresses)


def test_update_geo_location(db_session):
    """Test updating a geo location."""
    location = gis_service.geo_locations.create(
        db_session,
        GeoLocationCreate(
            name="Original Location",
            location_type=GeoLocationType.address,
            latitude=40.0,
            longitude=-74.0,
        ),
    )
    updated = gis_service.geo_locations.update(
        db_session,
        str(location.id),
        GeoLocationUpdate(name="Updated Location"),
    )
    assert updated.name == "Updated Location"


def test_delete_geo_location(db_session):
    """Test deleting a geo location."""
    location = gis_service.geo_locations.create(
        db_session,
        GeoLocationCreate(
            name="To Delete",
            location_type=GeoLocationType.address,
            latitude=39.0,
            longitude=-75.0,
        ),
    )
    gis_service.geo_locations.delete(db_session, str(location.id))
    db_session.refresh(location)
    assert location.is_active is False


def test_create_geo_layer(db_session):
    """Test creating a geo layer."""
    layer = gis_service.geo_layers.create(
        db_session,
        GeoLayerCreate(
            name="Service Areas",
            layer_key="service-areas",
            layer_type=GeoLayerType.polygons,
        ),
    )
    assert layer.name == "Service Areas"
    assert layer.layer_type == GeoLayerType.polygons


def test_list_geo_layers_by_type(db_session):
    """Test listing geo layers filtered by type."""
    gis_service.geo_layers.create(
        db_session,
        GeoLayerCreate(
            name="Polygon Layer",
            layer_key="polygon-layer",
            layer_type=GeoLayerType.polygons,
        ),
    )
    gis_service.geo_layers.create(
        db_session,
        GeoLayerCreate(
            name="Points Layer",
            layer_key="points-layer",
            layer_type=GeoLayerType.points,
        ),
    )

    polygons = gis_service.geo_layers.list(
        db_session,
        layer_type="polygons",
        source_type=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert all(l.layer_type == GeoLayerType.polygons for l in polygons)


def test_update_geo_layer(db_session):
    """Test updating a geo layer."""
    layer = gis_service.geo_layers.create(
        db_session,
        GeoLayerCreate(
            name="Original Layer",
            layer_key="original-layer",
            layer_type=GeoLayerType.points,
        ),
    )
    updated = gis_service.geo_layers.update(
        db_session,
        str(layer.id),
        GeoLayerUpdate(name="Renamed Layer"),
    )
    assert updated.name == "Renamed Layer"


def test_create_geo_area(db_session):
    """Test creating a geo area."""
    area = gis_service.geo_areas.create(
        db_session,
        GeoAreaCreate(
            name="Downtown Zone",
            area_type=GeoAreaType.service_area,
        ),
    )
    assert area.name == "Downtown Zone"
    assert area.area_type == GeoAreaType.service_area


def test_list_geo_areas_by_type(db_session):
    """Test listing geo areas filtered by type."""
    gis_service.geo_areas.create(
        db_session,
        GeoAreaCreate(
            name="Service Zone 1",
            area_type=GeoAreaType.service_area,
        ),
    )
    gis_service.geo_areas.create(
        db_session,
        GeoAreaCreate(
            name="Coverage Zone 1",
            area_type=GeoAreaType.coverage,
        ),
    )

    service_areas = gis_service.geo_areas.list(
        db_session,
        area_type="service_area",
        is_active=None,
        min_latitude=None,
        min_longitude=None,
        max_latitude=None,
        max_longitude=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert all(a.area_type == GeoAreaType.service_area for a in service_areas)


def test_update_geo_area(db_session):
    """Test updating a geo area."""
    area = gis_service.geo_areas.create(
        db_session,
        GeoAreaCreate(
            name="Original Area",
            area_type=GeoAreaType.service_area,
        ),
    )
    updated = gis_service.geo_areas.update(
        db_session,
        str(area.id),
        GeoAreaUpdate(name="Updated Area"),
    )
    assert updated.name == "Updated Area"


def test_delete_geo_area(db_session):
    """Test deleting a geo area."""
    area = gis_service.geo_areas.create(
        db_session,
        GeoAreaCreate(
            name="To Delete",
            area_type=GeoAreaType.service_area,
        ),
    )
    gis_service.geo_areas.delete(db_session, str(area.id))
    db_session.refresh(area)
    assert area.is_active is False


def test_get_geo_location(db_session):
    """Test getting a geo location by ID."""
    location = gis_service.geo_locations.create(
        db_session,
        GeoLocationCreate(
            name="Test Location",
            location_type=GeoLocationType.site,
            latitude=35.0,
            longitude=-80.0,
        ),
    )
    fetched = gis_service.geo_locations.get(db_session, str(location.id))
    assert fetched is not None
    assert fetched.id == location.id
    assert fetched.name == "Test Location"
