from __future__ import annotations

import uuid
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
from app.models.field_attachment import FieldAttachment
from app.models.network import FdhCabinet, FiberAccessPoint
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.field import fiber as field_fiber
from app.services.network.fiber_topology_field_observations import (
    FiberTopologyFieldObservationError,
    project_field_verification_evidence,
    record_fiber_field_observation,
)
from app.services.network.fiber_topology_identity_coverage import (
    reconcile_fiber_identity_coverage,
)


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage(
    db_session,
    *,
    asset_type: str = "fiber_access_point",
    external_id: str = "FAT-FIELD-1",
    created_at: datetime | None = None,
) -> FiberTopologyStagedFeature:
    observed_at = created_at or datetime.now(UTC)
    is_path = asset_type == "fiber_segment"
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_field_{uuid.uuid4().hex}",
        source_name="pytest-field-evidence.kmz",
        asset_type=asset_type,
        external_id_key="spanid" if is_path else "assetid",
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
        created_at=observed_at,
    )
    feature = FiberTopologyStagedFeature(
        batch=batch,
        row_number=1,
        asset_type=asset_type,
        external_id=external_id,
        display_name=external_id,
        geometry_type="LineString" if is_path else "Point",
        geometry_geojson=(
            {
                "type": "LineString",
                "coordinates": [[7.40, 9.00], [7.42, 9.02]],
            }
            if is_path
            else {"type": "Point", "coordinates": [7.40, 9.00]}
        ),
        source_properties={"test": True},
        content_sha256=_sha(),
        geometry_sha256=_sha(),
        match_status="new",
        blocker_codes=[],
        match_reasons=[],
        candidate_asset_ids=[],
        created_at=observed_at,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def _actor_and_job(db_session):
    user = SystemUser(
        first_name="Field",
        last_name="Verifier",
        display_name="Field Verifier",
        email=f"fiber-verifier-{uuid.uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    subscriber = Subscriber(
        first_name="Fiber",
        last_name="Customer",
        email=f"fiber-customer-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([user, subscriber])
    db_session.flush()
    technician = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-field-verifier",
    )
    work_order = WorkOrder(
        public_id=f"WO-FIELD-{uuid.uuid4().hex[:8]}",
        crm_work_order_id=f"crm-wo-{uuid.uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        title="Verify staged fiber source",
        status="in_progress",
        assigned_to_crm_person_id="crm-field-verifier",
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
    *,
    client_ref: uuid.UUID | None = None,
    verification_scope: str = "identity",
    outcome: str = "agrees",
    observed_external_label: str | None = "FAT field label",
    observed_at: datetime | None = None,
    **kwargs,
) -> FiberTopologyFieldObservation:
    return record_fiber_field_observation(
        db_session,
        staged_feature_id=feature.id,
        expected_feature_content_sha256=feature.content_sha256,
        work_order_id=work_order.id,
        recorded_by_technician_id=technician.id,
        recorded_by_person_id=technician.person_id,
        recorded_by_system_user_id=technician.system_user_id,
        verification_scope=verification_scope,
        outcome=outcome,
        observed_at=observed_at or datetime.now(UTC),
        client_ref=client_ref or uuid.uuid4(),
        observed_external_label=observed_external_label,
        **kwargs,
    )


def _attachment(
    db_session,
    technician: TechnicianProfile,
    work_order: WorkOrder,
) -> FieldAttachment:
    stored = StoredFile(
        entity_type="field_attachment",
        entity_id=str(work_order.id),
        original_filename="fiber-evidence.jpg",
        storage_key_or_relative_path=f"field/{uuid.uuid4()}.jpg",
        file_size=128,
        content_type="image/jpeg",
    )
    db_session.add(stored)
    db_session.flush()
    attachment = FieldAttachment(
        work_order_mirror_id=work_order.id,
        stored_file_id=stored.id,
        kind="photo",
        file_name="fiber-evidence.jpg",
        mime_type="image/jpeg",
        size_bytes=128,
        uploaded_by_technician_id=technician.id,
        uploaded_by_person_id=technician.person_id,
        uploaded_by_system_user_id=technician.system_user_id,
    )
    db_session.add(attachment)
    db_session.commit()
    return attachment


def test_record_is_exactly_idempotent_and_client_ref_cannot_be_reused(db_session):
    feature = _stage(db_session)
    _user, technician, work_order = _actor_and_job(db_session)
    client_ref = uuid.uuid4()
    observed_at = datetime.now(UTC)

    observation = _record(
        db_session,
        feature,
        technician,
        work_order,
        client_ref=client_ref,
        observed_at=observed_at,
        latitude=9.01,
        longitude=7.41,
        accuracy_m=3.5,
        instrument="GNSS receiver",
        measurement_payload={"label_photo_checked": True},
    )
    replay = _record(
        db_session,
        feature,
        technician,
        work_order,
        client_ref=client_ref,
        observed_at=observed_at,
        latitude=9.01,
        longitude=7.41,
        accuracy_m=3.5,
        instrument="GNSS receiver",
        measurement_payload={"label_photo_checked": True},
    )

    assert replay.id == observation.id
    assert observation.feature_content_sha256 == feature.content_sha256
    assert observation.work_order_public_id == work_order.public_id
    assert len(observation.claim_sha256) == 64
    assert len(observation.observation_sha256) == 64
    assert db_session.query(FiberTopologyFieldObservation).count() == 1

    with pytest.raises(FiberTopologyFieldObservationError, match="client_ref"):
        _record(
            db_session,
            feature,
            technician,
            work_order,
            client_ref=client_ref,
            observed_at=observed_at,
            notes="different immutable payload",
        )


def test_contradictory_current_facts_are_retained_and_projected(db_session):
    feature = _stage(db_session, external_id="FAT-FIELD-CONFLICT")
    _user, technician, work_order = _actor_and_job(db_session)

    first = _record(db_session, feature, technician, work_order)
    second = _record(
        db_session,
        feature,
        technician,
        work_order,
        outcome="conflicts",
        observed_external_label="Different FAT label",
    )
    evidence = project_field_verification_evidence(db_session, [feature])[
        str(feature.id)
    ]

    assert first.id != second.id
    assert evidence["current_observation_count"] == 2
    assert evidence["state"] == "conflicting_observations"
    assert evidence["scope_states"] == {"identity": "conflicting_observations"}


def test_new_source_content_supersedes_old_observation_and_rejects_stale_write(
    db_session,
):
    observed_at = datetime.now(UTC)
    first = _stage(
        db_session,
        external_id="FAT-FIELD-SUPERSEDED",
        created_at=observed_at,
    )
    _user, technician, work_order = _actor_and_job(db_session)
    old_observation = _record(db_session, first, technician, work_order)
    latest = _stage(
        db_session,
        external_id="FAT-FIELD-SUPERSEDED",
        created_at=observed_at + timedelta(seconds=1),
    )

    evidence = project_field_verification_evidence(db_session, [latest])[str(latest.id)]
    assert evidence["state"] == "superseded_only"
    assert evidence["superseded_observation_ids"] == [str(old_observation.id)]

    with pytest.raises(FiberTopologyFieldObservationError, match="newer staged"):
        _record(db_session, first, technician, work_order)


def test_path_endpoints_are_explicit_and_point_endpoint_scope_is_rejected(db_session):
    path = _stage(
        db_session,
        asset_type="fiber_segment",
        external_id="SPAN-FIELD-ENDPOINTS",
    )
    point = _stage(db_session, external_id="FAT-FIELD-SCOPE")
    _user, technician, work_order = _actor_and_job(db_session)
    cabinet = FdhCabinet(name="Field cabinet", code="CAB-FIELD")
    access_point = FiberAccessPoint(name="Field FAT", code="FAT-FIELD")
    db_session.add_all([cabinet, access_point])
    db_session.commit()

    observation = _record(
        db_session,
        path,
        technician,
        work_order,
        verification_scope="path_endpoints",
        observed_external_label=None,
        start_endpoint_type="fdh",
        start_endpoint_ref_id=cabinet.id,
        end_endpoint_type="fiber_access_point",
        end_endpoint_ref_id=access_point.id,
    )
    assert observation.start_endpoint_ref_id == cabinet.id
    assert observation.end_endpoint_ref_id == access_point.id

    with pytest.raises(FiberTopologyFieldObservationError, match="point assets"):
        _record(
            db_session,
            point,
            technician,
            work_order,
            verification_scope="start_endpoint",
            observed_external_label=None,
            start_endpoint_type="fdh",
            start_endpoint_ref_id=cabinet.id,
        )


def test_soft_deleted_attachment_projects_evidence_drift(db_session):
    feature = _stage(db_session, external_id="FAT-FIELD-ATTACHMENT")
    _user, technician, work_order = _actor_and_job(db_session)
    attachment = _attachment(db_session, technician, work_order)
    observation = _record(
        db_session,
        feature,
        technician,
        work_order,
        attachment_ids=[attachment.id],
    )

    current = project_field_verification_evidence(db_session, [feature])[
        str(feature.id)
    ]
    assert current["state"] == "current_agreement"
    assert current["drift_observation_ids"] == []

    attachment.is_active = False
    db_session.commit()
    drift = project_field_verification_evidence(db_session, [feature])[str(feature.id)]
    assert drift["state"] == "evidence_drift"
    assert drift["drift_observation_ids"] == [str(observation.id)]


def test_field_adapter_scopes_job_and_delegates_to_observation_owner(db_session):
    feature = _stage(db_session, external_id="FAT-FIELD-ADAPTER")
    user, _technician, work_order = _actor_and_job(db_session)

    observation = field_fiber.record_source_observation(
        db_session,
        _auth(user),
        work_order_public_id=work_order.public_id,
        staged_feature_id=str(feature.id),
        expected_feature_content_sha256=feature.content_sha256,
        verification_scope="presence",
        outcome="agrees",
        observed_at=datetime.now(UTC),
        client_ref=str(uuid.uuid4()),
    )
    rows = field_fiber.list_source_observations(
        db_session,
        _auth(user),
        work_order_public_id=work_order.public_id,
        staged_feature_id=str(feature.id),
    )

    assert [row.id for row in rows] == [observation.id]
    with pytest.raises(HTTPException) as exc:
        field_fiber.list_source_observations(
            db_session,
            _auth(user),
            work_order_public_id="WO-NOT-ASSIGNED",
        )
    assert exc.value.status_code == 404


def test_coverage_includes_field_evidence_without_turning_it_into_a_gate(db_session):
    feature = _stage(db_session, external_id="FAT-FIELD-COVERAGE")
    _user, technician, work_order = _actor_and_job(db_session)
    before = reconcile_fiber_identity_coverage(db_session)

    _record(
        db_session,
        feature,
        technician,
        work_order,
        verification_scope="presence",
        observed_external_label=None,
    )
    after = reconcile_fiber_identity_coverage(db_session)

    assert after.assets[0]["field_verification"]["state"] == "current_agreement"
    assert after.field_verification_counts["current_agreement"] == 1
    assert after.gates == before.gates
    assert after.ready_for_point_identity_cutover_review is False
