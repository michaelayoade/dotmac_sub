from app.schemas.gis import GeoLocationCreate
from app.schemas.qualification import CoverageAreaCreate
from app.services import gis as gis_service
from app.services import imports as import_service
from app.services import qualification as qualification_service


def test_geo_location_create_list(db_session):
    location = gis_service.geo_locations.create(
        db_session,
        GeoLocationCreate(
            name="Test POP",
            location_type="pop",
            latitude=6.5244,
            longitude=3.3792,
        ),
    )
    items = gis_service.geo_locations.list(
        db_session,
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
        limit=10,
        offset=0,
    )
    assert items[0].id == location.id


def test_coverage_area_create(db_session):
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [3.0, 6.0],
                [4.0, 6.0],
                [4.0, 7.0],
                [3.0, 7.0],
                [3.0, 6.0],
            ]
        ],
    }
    area = qualification_service.coverage_areas.create(
        db_session,
        CoverageAreaCreate(
            name="Zone A",
            geometry_geojson=polygon,
        ),
    )
    assert area.min_latitude is not None
    assert area.max_longitude is not None


def test_import_subscriber_custom_fields(db_session, subscriber):
    content = (
        "subscriber_id,key,value_type,value_text\n"
        f"{subscriber.id},plan,string,Gold\n"
    )
    created, errors = import_service.import_subscriber_custom_fields_from_csv(
        db_session, content
    )
    assert created == 1
    assert errors == []
