from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

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
from app.services.field import fiber as field_fiber
from app.services.network import fiber_topology_work_order_evidence_map
from app.services.network.fiber_topology_field_map import (
    project_fiber_field_verification_map,
)
from app.services.network.fiber_topology_field_observations import (
    record_fiber_field_observation,
)
from app.services.network.fiber_topology_work_order_evidence_map import (
    FiberTopologyWorkOrderEvidenceMapError,
    project_fiber_work_order_evidence_map,
)


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage(
    db_session,
    *,
    external_id: str,
    created_at: datetime | None = None,
    coordinates: list[float] | None = None,
) -> FiberTopologyStagedFeature:
    staged_at = created_at or datetime.now(UTC)
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_job_map_{uuid.uuid4().hex}",
        source_name="pytest-job-evidence-map.kmz",
        asset_type="fiber_access_point",
        external_id_key="assetid",
        file_sha256=_sha(),
        manifest_sha256=_sha(),
        status="staged",
        feature_count=1,
        blocker_count=0,
        candidate_count=0,
        unchanged_count=0,
        new_count=1,
        source_metadata={"test": True},
        created_by="pytest-stager",
        created_at=staged_at,
    )
    feature = FiberTopologyStagedFeature(
        batch=batch,
        row_number=1,
        asset_type="fiber_access_point",
        external_id=external_id,
        display_name=external_id,
        geometry_type="Point",
        geometry_geojson={
            "type": "Point",
            "coordinates": coordinates or [7.40, 9.00],
        },
        source_properties={"test": True},
        content_sha256=_sha(),
        geometry_sha256=_sha(),
        match_status="new",
        blocker_codes=[],
        match_reasons=[],
        candidate_asset_ids=[],
        created_at=staged_at,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def _actor_and_job(db_session, *, label: str):
    crm_person_id = f"crm-job-map-{label}-{uuid.uuid4().hex[:8]}"
    user = SystemUser(
        first_name=label,
        last_name="Verifier",
        display_name=f"{label} Verifier",
        email=f"job-map-{label.lower()}-{uuid.uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    subscriber = Subscriber(
        first_name=label,
        last_name="Customer",
        email=f"job-map-customer-{label.lower()}-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([user, subscriber])
    db_session.flush()
    technician = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
    )
    work_order = WorkOrder(
        public_id=f"WO-JOB-MAP-{label.upper()}-{uuid.uuid4().hex[:8]}",
        crm_work_order_id=f"crm-job-map-{label.lower()}-{uuid.uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        title=f"Verify {label} exact source evidence",
        status="in_progress",
        assigned_to_crm_person_id=crm_person_id,
        scheduled_start=datetime.now(UTC),
    )
    db_session.add_all([technician, work_order])
    db_session.commit()
    return user, technician, work_order


def _auth(user: SystemUser) -> dict[str, object]:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _record(
    db_session,
    feature: FiberTopologyStagedFeature,
    technician: TechnicianProfile,
    work_order: WorkOrder,
) -> FiberTopologyFieldObservation:
    return record_fiber_field_observation(
        db_session,
        staged_feature_id=feature.id,
        expected_feature_content_sha256=feature.content_sha256,
        work_order_id=work_order.id,
        recorded_by_technician_id=technician.id,
        recorded_by_person_id=technician.person_id,
        recorded_by_system_user_id=technician.system_user_id,
        verification_scope="presence",
        outcome="agrees",
        observed_at=datetime.now(UTC),
        client_ref=uuid.uuid4(),
        observed_external_label=feature.external_id,
        notes=f"Exact evidence for {work_order.public_id}",
    )


def test_empty_job_map_is_stable_and_does_not_infer_unobserved_features(db_session):
    _stage(db_session, external_id="FAT-JOB-MAP-UNOBSERVED")
    _user, _technician, work_order = _actor_and_job(db_session, label="Empty")
    before = db_session.query(FiberTopologyFieldObservation).count()

    report = project_fiber_work_order_evidence_map(
        db_session,
        work_order_id=work_order.id,
        expected_work_order_public_id=work_order.public_id,
    )
    replay = project_fiber_work_order_evidence_map(
        db_session,
        work_order_id=work_order.id,
        expected_work_order_public_id=work_order.public_id,
    )

    assert report.observation_count == 0
    assert report.feature_count == 0
    assert report.feature_collection == {"features": [], "type": "FeatureCollection"}
    assert replay.report_sha256 == report.report_sha256
    assert db_session.query(FiberTopologyFieldObservation).count() == before
    assert "ready" not in json.dumps(report.to_dict()).lower()
    assert "eligible" not in json.dumps(report.to_dict()).lower()


def test_job_map_filters_exact_observations_and_strips_other_job_evidence(db_session):
    feature = _stage(db_session, external_id="FAT-JOB-MAP-PRIVATE")
    _first_user, first_technician, first_job = _actor_and_job(db_session, label="First")
    _second_user, second_technician, second_job = _actor_and_job(
        db_session, label="Second"
    )
    first_observation = _record(db_session, feature, first_technician, first_job)
    _record(db_session, feature, second_technician, second_job)

    report = project_fiber_work_order_evidence_map(
        db_session,
        work_order_id=first_job.id,
        expected_work_order_public_id=first_job.public_id,
    )
    projected = report.feature_collection["features"][0]
    properties = projected["properties"]
    evidence = properties["work_order_evidence"]
    serialized = json.dumps(report.to_dict(), sort_keys=True)

    assert report.observation_count == 1
    assert report.feature_count == 1
    assert report.current_source_observation_count == 1
    assert report.superseded_source_observation_count == 0
    assert projected["geometry"] == feature.geometry_geojson
    assert evidence["context"] == "current_source"
    assert evidence["context_presentation"] == {
        "icon": "check",
        "label": "Current source",
        "tone": "positive",
        "value": "current_source",
    }
    assert properties["geometry_presentation"] == {
        "icon": "check",
        "label": "Exact source geometry",
        "tone": "positive",
        "value": "exact_geojson",
    }
    assert evidence["current_observations"][0]["observation_id"] == str(
        first_observation.id
    )
    assert evidence["current_observations"][0]["observation_sha256"] == (
        first_observation.observation_sha256
    )
    assert "field_verification" not in properties
    assert "current_work_orders" not in properties
    assert "superseded_work_orders" not in properties
    assert second_job.public_id not in serialized
    assert str(second_job.id) not in serialized
    assert len(properties["map_feature_sha256"]) == 64
    assert len(properties["work_order_evidence_sha256"]) == 64
    assert len(properties["work_order_map_feature_sha256"]) == 64


def test_job_map_keeps_superseded_observation_distinct_from_latest_exact_geometry(
    db_session,
):
    staged_at = datetime.now(UTC)
    observed = _stage(
        db_session,
        external_id="FAT-JOB-MAP-SUPERSEDED",
        created_at=staged_at,
        coordinates=[7.40, 9.00],
    )
    _user, technician, work_order = _actor_and_job(db_session, label="Superseded")
    observation = _record(db_session, observed, technician, work_order)
    latest = _stage(
        db_session,
        external_id="FAT-JOB-MAP-SUPERSEDED",
        created_at=staged_at + timedelta(seconds=1),
        coordinates=[7.45, 9.05],
    )

    report = project_fiber_work_order_evidence_map(
        db_session,
        work_order_id=work_order.id,
        expected_work_order_public_id=work_order.public_id,
    )
    projected = report.feature_collection["features"][0]
    properties = projected["properties"]
    evidence = properties["work_order_evidence"]
    historical = evidence["superseded_observations"][0]

    assert report.current_source_observation_count == 0
    assert report.superseded_source_observation_count == 1
    assert report.evidence_context_counts["superseded_source"] == 1
    assert projected["id"] == str(latest.id)
    assert projected["geometry"] == latest.geometry_geojson
    assert properties["content_sha256"] == latest.content_sha256
    assert evidence["context"] == "superseded_source"
    assert historical["observation_id"] == str(observation.id)
    assert historical["staged_feature_id"] == str(observed.id)
    assert historical["feature_content_sha256"] == observed.content_sha256
    assert historical["feature_content_sha256"] != properties["content_sha256"]


def test_job_map_keeps_current_and_superseded_evidence_distinct_on_one_feature(
    db_session,
):
    staged_at = datetime.now(UTC)
    first = _stage(
        db_session,
        external_id="FAT-JOB-MAP-BOTH",
        created_at=staged_at,
    )
    _user, technician, work_order = _actor_and_job(db_session, label="Both")
    old_observation = _record(db_session, first, technician, work_order)
    latest = _stage(
        db_session,
        external_id="FAT-JOB-MAP-BOTH",
        created_at=staged_at + timedelta(seconds=1),
    )
    current_observation = _record(db_session, latest, technician, work_order)

    report = project_fiber_work_order_evidence_map(
        db_session,
        work_order_id=work_order.id,
        expected_work_order_public_id=work_order.public_id,
    )
    evidence = report.feature_collection["features"][0]["properties"][
        "work_order_evidence"
    ]

    assert report.observation_count == 2
    assert report.current_source_observation_count == 1
    assert report.superseded_source_observation_count == 1
    assert report.evidence_context_counts["current_and_superseded_source"] == 1
    assert evidence["context"] == "current_and_superseded_source"
    assert evidence["current_observations"][0]["observation_id"] == str(
        current_observation.id
    )
    assert evidence["superseded_observations"][0]["observation_id"] == str(
        old_observation.id
    )


def test_job_map_fails_closed_when_an_observation_is_not_in_phase21_overlay(
    db_session, monkeypatch
):
    feature = _stage(db_session, external_id="FAT-JOB-MAP-MISSING")
    _user, technician, work_order = _actor_and_job(db_session, label="Missing")
    observation = _record(db_session, feature, technician, work_order)
    source_map = project_fiber_field_verification_map(db_session)
    missing_map = replace(
        source_map,
        feature_collection={"features": [], "type": "FeatureCollection"},
    )
    monkeypatch.setattr(
        fiber_topology_work_order_evidence_map,
        "project_fiber_field_verification_map",
        lambda _db: missing_map,
    )

    with pytest.raises(
        FiberTopologyWorkOrderEvidenceMapError,
        match="must map to exactly one",
    ) as exc:
        project_fiber_work_order_evidence_map(
            db_session,
            work_order_id=work_order.id,
            expected_work_order_public_id=work_order.public_id,
        )
    assert str(observation.id) in str(exc.value)


def test_field_adapter_is_get_only_and_enforces_technician_job_scope(db_session):
    feature = _stage(db_session, external_id="FAT-JOB-MAP-SCOPED")
    allowed_user, allowed_technician, allowed_job = _actor_and_job(
        db_session, label="Allowed"
    )
    denied_user, _denied_technician, _denied_job = _actor_and_job(
        db_session, label="Denied"
    )
    _record(db_session, feature, allowed_technician, allowed_job)

    report = field_fiber.get_work_order_evidence_map(
        db_session,
        _auth(allowed_user),
        work_order_public_id=allowed_job.public_id,
    )
    assert report.work_order_public_id == allowed_job.public_id

    with pytest.raises(HTTPException) as exc:
        field_fiber.get_work_order_evidence_map(
            db_session,
            _auth(denied_user),
            work_order_public_id=allowed_job.public_id,
        )
    assert exc.value.status_code == 404
