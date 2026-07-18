from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.dispatch import TechnicianProfile
from app.models.fiber_topology_field_observation import (
    FiberTopologyFieldObservation,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.network import fiber_topology_field_map
from app.services.network.fiber_topology_field_map import (
    FiberTopologyFieldMapError,
    project_fiber_field_verification_map,
)
from app.services.network.fiber_topology_field_observations import (
    record_fiber_field_observation,
)
from app.services.network.fiber_topology_field_worklist import (
    reconcile_fiber_field_worklist,
)
from app.web.admin import network_fiber_plant


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage_features(
    db_session,
    definitions: list[dict[str, object]],
) -> list[FiberTopologyStagedFeature]:
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_field_map_{uuid.uuid4().hex}",
        source_name="pytest-field-map.kmz",
        asset_type="mixed",
        external_id_key="assetid",
        file_sha256=_sha(),
        manifest_sha256=_sha(),
        status="staged",
        feature_count=len(definitions),
        blocker_count=0,
        candidate_count=0,
        unchanged_count=0,
        new_count=len(definitions),
        source_metadata={"test": True},
        created_by="pytest-stager",
        created_at=datetime.now(UTC),
    )
    features: list[FiberTopologyStagedFeature] = []
    for row_number, definition in enumerate(definitions, start=1):
        feature = FiberTopologyStagedFeature(
            batch=batch,
            row_number=row_number,
            asset_type=str(definition["asset_type"]),
            external_id=str(definition["external_id"]),
            display_name=str(definition["external_id"]),
            geometry_type=str(definition["geometry_type"]),
            geometry_geojson=definition["geometry"],
            source_properties={"test": True},
            content_sha256=_sha(),
            geometry_sha256=_sha(),
            match_status="blocked" if definition.get("blocked") else "new",
            blocker_codes=(
                ["missing_supported_geometry"] if definition.get("blocked") else []
            ),
            match_reasons=[],
            candidate_asset_ids=[],
            created_at=datetime.now(UTC),
        )
        features.append(feature)
    db_session.add(batch)
    db_session.commit()
    for feature in features:
        db_session.refresh(feature)
    return features


def _actor_and_job(db_session) -> tuple[TechnicianProfile, WorkOrder]:
    user = SystemUser(
        first_name="Map",
        last_name="Verifier",
        display_name="Map Verifier",
        email=f"map-verifier-{uuid.uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    subscriber = Subscriber(
        first_name="Map",
        last_name="Customer",
        email=f"map-customer-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([user, subscriber])
    db_session.flush()
    technician = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-map-verifier",
    )
    work_order = WorkOrder(
        public_id=f"WO-MAP-{uuid.uuid4().hex[:8]}",
        crm_work_order_id=f"crm-map-wo-{uuid.uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        title="Verify exact staged map feature",
        status="in_progress",
        assigned_to_crm_person_id="crm-map-verifier",
        scheduled_start=datetime.now(UTC),
    )
    db_session.add_all([technician, work_order])
    db_session.commit()
    return technician, work_order


def test_empty_field_map_is_stable_complete_and_read_only(db_session):
    before = db_session.query(FiberTopologyFieldObservation).count()

    report = project_fiber_field_verification_map(db_session)
    replay = project_fiber_field_verification_map(db_session)

    assert report.staged_feature_count == 0
    assert report.feature_collection == {"features": [], "type": "FeatureCollection"}
    assert report.bbox is None
    assert report.geometry_presentation_counts == {
        "exact_geojson": 0,
        "source_geometry_unrenderable": 0,
    }
    assert replay.overlay_sha256 == report.overlay_sha256
    assert replay.worklist_report_sha256 == report.worklist_report_sha256
    assert db_session.query(FiberTopologyFieldObservation).count() == before
    payload_text = str(report.to_dict()).lower()
    assert "ready" not in payload_text
    assert "eligible" not in payload_text


def test_field_map_preserves_exact_geojson_complete_cohort_hashes_and_bbox(
    db_session,
):
    exact_geometries = {
        "FAT-MAP-POINT": {"type": "Point", "coordinates": [7.40, 9.00]},
        "SPAN-MAP-LINE": {
            "type": "LineString",
            "coordinates": [[7.41, 9.01], [7.45, 9.05]],
        },
        "CAB-MAP-POLYGON": {
            "type": "Polygon",
            "coordinates": [[[7.42, 9.02], [7.43, 9.02], [7.43, 9.03], [7.42, 9.02]]],
        },
        "FAT-MAP-BLOCKED": {"type": "GeometryCollection", "geometries": []},
    }
    staged = _stage_features(
        db_session,
        [
            {
                "asset_type": "fiber_access_point",
                "external_id": "FAT-MAP-POINT",
                "geometry_type": "Point",
                "geometry": exact_geometries["FAT-MAP-POINT"],
            },
            {
                "asset_type": "fiber_segment",
                "external_id": "SPAN-MAP-LINE",
                "geometry_type": "LineString",
                "geometry": exact_geometries["SPAN-MAP-LINE"],
            },
            {
                "asset_type": "fdh_cabinet",
                "external_id": "CAB-MAP-POLYGON",
                "geometry_type": "Polygon",
                "geometry": exact_geometries["CAB-MAP-POLYGON"],
            },
            {
                "asset_type": "fiber_access_point",
                "external_id": "FAT-MAP-BLOCKED",
                "geometry_type": "Unknown",
                "geometry": exact_geometries["FAT-MAP-BLOCKED"],
                "blocked": True,
            },
        ],
    )

    report = project_fiber_field_verification_map(db_session)
    features = {
        feature["properties"]["external_id"]: feature
        for feature in report.feature_collection["features"]
    }

    assert report.staged_feature_count == 4
    assert len(features) == 4
    assert report.bbox == [7.4, 9.0, 7.45, 9.05]
    assert report.geometry_presentation_counts == {
        "exact_geojson": 3,
        "source_geometry_unrenderable": 1,
    }
    for feature in staged:
        projected = features[feature.external_id]
        properties = projected["properties"]
        assert projected["geometry"] == exact_geometries[feature.external_id]
        assert properties["content_sha256"] == feature.content_sha256
        assert properties["geometry_sha256"] == feature.geometry_sha256
        assert properties["priority"] == "p4_unobserved"
        assert len(properties["row_sha256"]) == 64
        assert len(properties["map_feature_sha256"]) == 64


def test_field_map_propagates_owner_job_context_and_changes_exact_overlay_hash(
    db_session,
):
    feature = _stage_features(
        db_session,
        [
            {
                "asset_type": "fiber_access_point",
                "external_id": "FAT-MAP-JOB",
                "geometry_type": "Point",
                "geometry": {"type": "Point", "coordinates": [7.40, 9.00]},
            }
        ],
    )[0]
    technician, work_order = _actor_and_job(db_session)
    before = project_fiber_field_verification_map(db_session)

    record_fiber_field_observation(
        db_session,
        staged_feature_id=feature.id,
        expected_feature_content_sha256=feature.content_sha256,
        work_order_id=work_order.id,
        recorded_by_technician_id=technician.id,
        recorded_by_person_id=technician.person_id,
        recorded_by_system_user_id=technician.system_user_id,
        verification_scope="identity",
        outcome="agrees",
        observed_at=datetime.now(UTC),
        client_ref=uuid.uuid4(),
        observed_external_label="FAT-MAP-JOB",
    )
    after = project_fiber_field_verification_map(db_session)
    replay = project_fiber_field_verification_map(db_session)
    properties = after.feature_collection["features"][0]["properties"]

    assert before.overlay_sha256 != after.overlay_sha256
    assert properties["verification_state"] == "current_agreement"
    assert properties["priority"] == "p6_current_agreement"
    assert properties["current_work_orders"] == [
        {
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
        }
    ]
    assert properties["superseded_work_orders"] == []
    assert replay.overlay_sha256 == after.overlay_sha256


def test_field_map_fails_closed_on_worklist_source_hash_drift(db_session, monkeypatch):
    feature = _stage_features(
        db_session,
        [
            {
                "asset_type": "fiber_access_point",
                "external_id": "FAT-MAP-DRIFT",
                "geometry_type": "Point",
                "geometry": {"type": "Point", "coordinates": [7.40, 9.00]},
            }
        ],
    )[0]
    stale_worklist = reconcile_fiber_field_worklist(db_session)
    feature.content_sha256 = _sha()
    db_session.commit()
    monkeypatch.setattr(
        fiber_topology_field_map,
        "reconcile_fiber_field_worklist",
        lambda _db: stale_worklist,
    )

    with pytest.raises(FiberTopologyFieldMapError, match="content_sha256"):
        project_fiber_field_verification_map(db_session)


def test_admin_field_map_route_is_get_only_complete_projection():
    routes = {route.path: route for route in network_fiber_plant.router.routes}
    route = routes["/network/fiber-field-verification-map"]
    template = Path("templates/admin/network/fiber/field_verification_map.html")

    assert route.methods == {"GET"}
    assert template.exists()
    content = template.read_text()
    assert "complete read-only overlay" in content
    assert "exact staged source GeoJSON" in content
    assert "field_map.feature_collection" in content
    assert "Current-source work orders" in content
    assert "Superseded-source work orders" in content
    assert "Filters change only this browser view" in content
    assert "<form" not in content
