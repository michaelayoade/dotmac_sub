"""Vendor proposed-route authoring UI and projection behavior."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
    Vendor,
)
from app.schemas.vendor_portal import VendorRouteRevisionCreate
from app.services import network_map, vendor_routes_api
from app.services.field.map_assets import (
    VENDOR_ROUTE_AUTHORING_POI_FILTERS,
)
from app.services.ui_contracts import Action
from app.services.vendor_portal_operations import (
    VENDOR_ROUTE_AUTHORING_LAYER_FILTERS,
    VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS,
    VENDOR_ROUTE_AUTHORING_STATUS_FILTERS,
    _serialize_quote,
)
from app.web import vendor_portal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (PROJECT_ROOT / "templates/vendor/project_detail.html").read_text(
    encoding="utf-8"
)
AUTHORING_JS = (PROJECT_ROOT / "static/js/vendor-route-authoring.js").read_text(
    encoding="utf-8"
)
ADMIN_AUTHORING_TEMPLATE = (
    PROJECT_ROOT / "templates/admin/vendors/route_authoring.html"
).read_text(encoding="utf-8")
ADMIN_ROUTE_VIEW_TEMPLATE = (
    PROJECT_ROOT / "templates/admin/vendors/route_view.html"
).read_text(encoding="utf-8")
ASBUILT_JS = (PROJECT_ROOT / "static/js/vendor-asbuilt-map.js").read_text(
    encoding="utf-8"
)


def _route_payload(coordinates: list[list[float]]) -> VendorRouteRevisionCreate:
    return VendorRouteRevisionCreate(
        geojson={"type": "LineString", "coordinates": coordinates},
        length_meters=125.5,
    )


def test_route_payload_requires_a_valid_linestring() -> None:
    payload = _route_payload([[7.4, 9.0], [7.5, 9.1]])

    assert payload.geojson == {
        "type": "LineString",
        "coordinates": [[7.4, 9.0], [7.5, 9.1]],
    }
    assert payload.length_meters == pytest.approx(15_627.5, abs=1)
    assert payload.length_meters != 125.5

    invalid_geometries = (
        {"type": "Point", "coordinates": [7.4, 9.0]},
        {"type": "LineString", "coordinates": [[7.4, 9.0]]},
        {"type": "LineString", "coordinates": [[181, 9.0], [7.5, 9.1]]},
        {"type": "LineString", "coordinates": [[7.4, 91], [7.5, 9.1]]},
        {"type": "LineString", "coordinates": [[7.4, 9.0], [float("nan"), 9.1]]},
    )
    for geometry in invalid_geometries:
        with pytest.raises(ValidationError):
            VendorRouteRevisionCreate(geojson=geometry)
    with pytest.raises(ValidationError):
        VendorRouteRevisionCreate(
            geojson=payload.geojson,
            length_meters=float("inf"),
        )


def test_web_payload_maps_invalid_geometry_to_safe_validation_error() -> None:
    with pytest.raises(HTTPException) as exc:
        vendor_portal._route_revision_payload(
            '{"type":"LineString","coordinates":[[7.4,9.0]]}',
            None,
        )

    assert exc.value.status_code == 422
    assert "at least two map points" in str(exc.value.detail)


def _revision(
    revision_id,
    *,
    revision_number: int,
    status: str,
    length_meters: float | None,
    review_notes: str | None,
) -> SimpleNamespace:
    """A revision shaped for both projections ``_serialize_quote`` builds.

    ``route_authoring`` (this branch) only needs the presentation fields, but the
    same call also builds the staff ``route_revisions`` review list, which walks
    ``revision.quote`` out to the project and vendor. A fixture that omits those
    would only pass by accident of which projection got read.
    """
    return SimpleNamespace(
        id=revision_id,
        quote_id=None,
        revision_number=revision_number,
        status=status,
        length_meters=length_meters,
        review_notes=review_notes,
        submitted_at=None,
        submitted_by_person_id=None,
        reviewed_at=None,
        reviewed_by_person_id=None,
        route_geom=None,
        review_events=[],
        quote=SimpleNamespace(
            vendor_id=None,
            vendor=SimpleNamespace(name="Vendor"),
            project=SimpleNamespace(
                id=None, project=SimpleNamespace(name=None, code=None)
            ),
        ),
    )


def test_quote_projection_owns_route_status_and_actions() -> None:
    draft_id = uuid4()
    submitted_id = uuid4()
    quote = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        vendor_id=uuid4(),
        status="draft",
        currency="NGN",
        subtotal=0,
        vat_rate_percent=0,
        tax_total=0,
        total=0,
        valid_from=None,
        valid_until=None,
        submitted_at=None,
        reviewed_at=None,
        review_notes=None,
        line_items=[],
        route_revisions=[
            _revision(
                draft_id,
                revision_number=1,
                status=ProposedRouteRevisionStatus.draft.value,
                length_meters=100.0,
                review_notes=None,
            ),
            _revision(
                submitted_id,
                revision_number=2,
                status=ProposedRouteRevisionStatus.submitted.value,
                length_meters=None,
                review_notes="Awaiting survey evidence",
            ),
        ],
    )

    projection = _serialize_quote(quote)["route_authoring"]

    assert projection.create_action.allowed is True
    assert [item.id for item in projection.revisions] == [submitted_id, draft_id]
    assert projection.revisions[0].status.label == "Submitted"
    assert projection.revisions[0].submit_action.allowed is False
    assert projection.revisions[1].length_label == "100.0 m"
    assert projection.revisions[1].submit_action.allowed is True


def test_vendor_route_authoring_filter_contracts_are_explicit() -> None:
    assert all(
        option.selected_by_default for option in VENDOR_ROUTE_AUTHORING_LAYER_FILTERS
    )
    assert all(
        option.selected_by_default for option in VENDOR_ROUTE_AUTHORING_STATUS_FILTERS
    )
    assert tuple(
        option.value_meters
        for option in VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS
        if option.selected_by_default
    ) == (1000,)
    assert all(option.label for option in VENDOR_ROUTE_AUTHORING_LAYER_FILTERS)
    assert all(option.label for option in VENDOR_ROUTE_AUTHORING_STATUS_FILTERS)
    assert all(option.label for option in VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS)


def test_vendor_route_planner_has_no_global_plant_projection() -> None:
    assert not hasattr(network_map, "build_vendor_route_planning_map_projection")
    assert "plantLookupEnabled: false" in TEMPLATE


def test_authoring_ui_draws_saves_and_submits_owned_revisions() -> None:
    assert 'id="route-author-map"' in TEMPLATE
    assert 'id="route-author-geojson"' in TEMPLATE
    assert "/static/js/vendor-route-authoring.js" in TEMPLATE
    assert "VendorRouteAuthoring.mount" in TEMPLATE
    assert "config.plantLookupEnabled === false" in AUTHORING_JS
    create_source = inspect.getsource(vendor_portal.vendor_create_route_revision)
    submit_source = inspect.getsource(vendor_portal.vendor_submit_route_revision)
    assert "CreateVendorRouteRevisionCommand(" in create_source
    assert "SubmitVendorRouteRevisionCommand(" in submit_source
    assert "release_read_transaction(db)" in create_source
    assert "release_read_transaction(db)" in submit_source


def test_authoring_filter_contracts_render_in_project_template() -> None:
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
    rendered = templates.env.get_template("vendor/project_detail.html").render(
        request=SimpleNamespace(
            state=SimpleNamespace(
                csrf_token="test-token",
                auth={"permission_keys": ["*"]},
            )
        ),
        vendor=SimpleNamespace(name="Install Co"),
        project=SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            project_code="PRJ-1",
            project_name="Vendor map project",
            status="assigned",
            lifecycle_action=None,
            lifecycle_events=[],
            as_built_action=Action(
                key="submit_as_built",
                label="Submit as-built",
                allowed=True,
            ),
            as_built_submissions=[],
        ),
        quote=SimpleNamespace(
            id=uuid4(),
            status="draft",
            currency="NGN",
            total="0.00",
            line_items=[],
            edit_action=Action(key="edit_quote", label="Edit quote", allowed=True),
            route_authoring=SimpleNamespace(
                create_action=Action(
                    key="create_route_revision",
                    label="Save route draft",
                    allowed=True,
                ),
                revisions=[],
            ),
        ),
        invoice=None,
        route_geojson={"type": "FeatureCollection", "features": []},
        route_planning_map_geojson={"type": "FeatureCollection", "features": []},
        supply=SimpleNamespace(
            material_request_action=Action(
                key="request_material",
                label="Request material",
                allowed=False,
                reason="Not available in this test.",
            ),
            material_releases=[],
            advance_quote_total=None,
            advance_currency="NGN",
            advance_request_action=Action(
                key="request_advance",
                label="Request advance",
                allowed=False,
                reason="Not available in this test.",
            ),
            advances=[],
        ),
        vendor_work_orders=[],
        can_propose_closure=False,
        vendor_route_authoring_layer_filters=VENDOR_ROUTE_AUTHORING_LAYER_FILTERS,
        vendor_route_authoring_poi_filters=VENDOR_ROUTE_AUTHORING_POI_FILTERS,
        vendor_route_authoring_radius_options=VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS,
        vendor_route_authoring_status_filters=VENDOR_ROUTE_AUTHORING_STATUS_FILTERS,
        message=None,
    )

    assert 'id="route-author-map"' in rendered
    assert "plantLookupEnabled: false" in rendered
    assert "Open an assigned work order" in rendered
    assert 'id="route-author-poi-nearby"' not in rendered
    assert 'id="route-author-search"' not in rendered
    assert "data-route-plant-filter=" not in rendered


def test_vendor_route_context_does_not_include_competing_vendor_routes(
    db_session,
    monkeypatch,
) -> None:
    project = Project(name="Scoped vendor route context")
    vendor = Vendor(name="Visible Vendor", code=f"VV-{uuid4().hex[:8]}")
    competitor = Vendor(name="Hidden Vendor", code=f"HV-{uuid4().hex[:8]}")
    db_session.add_all([project, vendor, competitor])
    db_session.flush()
    installation = InstallationProject(project_id=project.id)
    db_session.add(installation)
    db_session.flush()
    own_quote = ProjectQuote(project_id=installation.id, vendor_id=vendor.id)
    other_quote = ProjectQuote(project_id=installation.id, vendor_id=competitor.id)
    db_session.add_all([own_quote, other_quote])
    db_session.flush()
    own_revision = ProposedRouteRevision(
        quote_id=own_quote.id,
        revision_number=1,
    )
    other_revision = ProposedRouteRevision(
        quote_id=other_quote.id,
        revision_number=1,
    )
    db_session.add_all([own_revision, other_revision])
    db_session.commit()
    monkeypatch.setattr(
        vendor_routes_api,
        "_geom_to_geojson",
        lambda _db, _geom: {
            "type": "LineString",
            "coordinates": [[7.4, 9.0], [7.5, 9.1]],
        },
    )

    result = vendor_routes_api.build_vendor_project_route_geojson(
        db_session,
        str(installation.id),
        str(vendor.id),
    )

    proposed_ids = {
        feature["properties"]["id"]
        for feature in result["features"]
        if feature["properties"]["kind"] == "proposed"
    }
    assert proposed_ids == {str(own_revision.id)}
    assert str(other_revision.id) not in proposed_ids
