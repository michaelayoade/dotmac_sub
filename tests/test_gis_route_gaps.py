from pathlib import Path
from types import SimpleNamespace

from starlette.routing import Match

from app.api import gis as gis_api
from app.models.gis import GeoAreaType
from app.web.admin import gis as web_gis


def _matched_endpoint(router, path: str, method: str = "GET"):
    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    for route in router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.endpoint
    return None


def test_locations_nearby_route_is_not_shadowed() -> None:
    endpoint = _matched_endpoint(gis_api.router, "/gis/locations/nearby")
    assert endpoint is gis_api.find_nearby_locations


def test_areas_containing_point_route_is_not_shadowed() -> None:
    endpoint = _matched_endpoint(gis_api.router, "/gis/areas/containing-point")
    assert endpoint is gis_api.find_areas_containing_point


def test_admin_gis_registers_layer_edit_routes() -> None:
    paths = {
        (route.path, tuple(sorted(route.methods or [])))
        for route in web_gis.router.routes
    }
    assert ("/gis/layers/{layer_id}/edit", ("GET",)) in paths
    assert ("/gis/layers/{layer_id}/edit", ("POST",)) in paths
    assert ("/gis/layers/{layer_id}/delete", ("POST",)) in paths
    assert ("/gis/areas/{area_id}/delete", ("POST",)) in paths
    assert ("/gis/locations/{location_id}/delete", ("POST",)) in paths


def test_coverage_check_defaults_to_serviceable_area_types(monkeypatch) -> None:
    monkeypatch.setattr(
        gis_api.gis_service.geo_areas,
        "find_containing",
        lambda **_: [
            SimpleNamespace(id="a", name="Coverage", area_type=GeoAreaType.coverage),
            SimpleNamespace(id="b", name="Region", area_type=GeoAreaType.region),
            SimpleNamespace(id="c", name="Service", area_type=GeoAreaType.service_area),
        ],
    )

    result = gis_api.coverage_check(
        latitude=9.0, longitude=7.0, area_type=None, db=object()
    )

    assert result["covered"] is True
    assert result["count"] == 2
    assert [item["area_type"] for item in result["matching_areas"]] == [
        "coverage",
        "service_area",
    ]


def test_gis_dashboard_template_exposes_area_edit_link() -> None:
    template = Path("templates/admin/gis/index.html").read_text()
    assert "/admin/gis/areas/{{ area.id }}/edit" in template


def test_gis_dashboard_template_renders_area_and_layer_overlays() -> None:
    template = Path("templates/admin/gis/index.html").read_text()
    assert "var areaFeatures = {{ area_features | tojson }}" in template
    assert "var layerOverlays = {{ layer_overlays | tojson }}" in template
    assert "loadLayerOverlay(layerMeta);" in template
    assert 'id="gisOverlayAlerts"' in template
    assert "reportOverlayFailure(layerMeta.name);" in template


def test_gis_dashboard_template_exposes_delete_actions_and_legend() -> None:
    template = Path("templates/admin/gis/index.html").read_text()
    assert 'action="/admin/gis/locations/{{ loc.id }}/delete"' in template
    assert 'action="/admin/gis/areas/{{ area.id }}/delete"' in template
    assert 'action="/admin/gis/layers/{{ layer.id }}/delete"' in template
    assert "Legend" in template


def test_area_form_template_includes_inline_polygon_editor() -> None:
    template = Path("templates/admin/gis/area_form.html").read_text()
    assert 'id="areaEditorMap"' in template
    assert "leaflet-draw" in template
    assert "syncTextarea()" in template
    assert "type: 'MultiPolygon'" in template
    assert "geometry.type === 'MultiPolygon'" in template
