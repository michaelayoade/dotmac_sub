from __future__ import annotations

from types import SimpleNamespace

from app.models.gis import GeoAreaType, GeoLocationType
from app.services import web_gis


def test_list_page_data_counts_coverage_areas(monkeypatch) -> None:
    class _FakeGeoLocations:
        def list(self, **kwargs):
            return ["loc-1"]

    class _FakeGeoAreas:
        def list(self, **kwargs):
            return [
                SimpleNamespace(area_type=GeoAreaType.coverage),
                SimpleNamespace(area_type=GeoAreaType.service_area),
                SimpleNamespace(area_type=GeoAreaType.region),
            ]

    class _FakeGeoLayers:
        def list(self, **kwargs):
            return ["layer-1"]

    monkeypatch.setattr(web_gis.gis_service, "geo_locations", _FakeGeoLocations())
    monkeypatch.setattr(web_gis.gis_service, "geo_areas", _FakeGeoAreas())
    monkeypatch.setattr(web_gis.gis_service, "geo_layers", _FakeGeoLayers())

    data = web_gis.list_page_data(db=object(), tab="areas")

    assert data["active_tab"] == "areas"
    assert data["locations"] == ["loc-1"]
    assert data["layers"] == ["layer-1"]
    assert data["coverage_areas"] == 2


def test_location_payload_builders() -> None:
    create_payload = web_gis.build_location_create_payload(
        name="HQ",
        location_type="site",
        latitude=1.2,
        longitude=3.4,
        notes="",
        is_active="true",
    )
    assert create_payload.location_type == GeoLocationType.site
    assert create_payload.notes is None
    assert create_payload.is_active is True

    update_payload = web_gis.build_location_update_payload(
        name="HQ2",
        location_type="custom",
        latitude=5.6,
        longitude=7.8,
        notes="Updated",
        is_active="false",
    )
    assert update_payload.location_type == GeoLocationType.custom
    assert update_payload.notes == "Updated"
    assert update_payload.is_active is False
