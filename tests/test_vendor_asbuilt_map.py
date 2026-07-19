"""The vendor as-built capture is a map, not a raw GeoJSON textarea.

Field techs trace the route they built by tapping the map or dropping their GPS
position; the drawn line is serialized to the existing ``geojson`` submit field.
The detail route feeds the proposed route as tracing context.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.web import vendor_portal

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates/vendor/project_detail.html"
).read_text(encoding="utf-8")


def test_asbuilt_uses_an_interactive_map_not_a_geojson_textarea():
    # The raw GeoJSON textarea the vendor used to paste into is gone.
    assert 'name="geojson" required rows=' not in TEMPLATE
    assert '{"type":"LineString","coordinates"' not in TEMPLATE  # old placeholder
    # Replaced by a leaflet map + a hidden geojson field the JS populates.
    assert 'id="asbuilt-map"' in TEMPLATE
    assert 'id="ab-geojson"' in TEMPLATE
    assert "/static/vendor/leaflet/leaflet.js" in TEMPLATE
    assert "/static/vendor/leaflet/leaflet.css" in TEMPLATE


def test_asbuilt_offers_gps_capture_and_serializes_a_linestring():
    # GPS "add my location" for on-site capture.
    assert 'id="ab-locate"' in TEMPLATE
    assert "navigator.geolocation" in TEMPLATE
    # Drawn vertices [lat, lng] are emitted as GeoJSON LineString [lng, lat].
    assert "type: 'LineString'" in TEMPLATE
    # Submit stays gated until a real line exists.
    assert "points.length < 2" in TEMPLATE
    assert "map.on('click'" in TEMPLATE


def test_detail_route_feeds_proposed_route_context():
    # The proposed route geometry is rendered server-side (vendor auth is
    # ownership-based, so it does not call the admin route API).
    source = inspect.getsource(vendor_portal.vendor_project_detail)
    assert "route_geojson" in source
    assert "build_project_route_geojson" in source
    assert "route_geojson" in TEMPLATE  # and the template consumes it
