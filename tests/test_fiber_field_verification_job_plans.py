from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dispatch import router
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
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
from app.schemas.dispatch import WorkOrderHeaderUpdate
from app.services.auth_dependencies import require_user_auth
from app.services.network.fiber_field_verification_job_plans import (
    FiberFieldVerificationJobPlanError,
    execute_fiber_field_verification_job_plan,
    preview_fiber_field_verification_job_plan,
)
from app.services.network.fiber_topology_field_observations import (
    FiberTopologyFieldObservationError,
    record_fiber_field_observation,
)
from app.services.network.fiber_topology_field_worklist import (
    reconcile_fiber_field_worklist,
)
from app.services.work_order_commands import work_order_commands


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage(
    db_session,
    external_id: str,
    *,
    created_at: datetime | None = None,
) -> FiberTopologyStagedFeature:
    observed_at = created_at or datetime.now(UTC)
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_job_plan_{uuid.uuid4().hex[:8]}",
        source_name="pytest-field-job-plan.kmz",
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
        created_at=observed_at,
    )
    feature = FiberTopologyStagedFeature(
        batch=batch,
        row_number=1,
        asset_type="fiber_access_point",
        external_id=external_id,
        display_name=external_id,
        geometry_type="Point",
        geometry_geojson={"type": "Point", "coordinates": [7.4, 9.0]},
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


def _subscriber(db_session) -> Subscriber:
    row = Subscriber(
        first_name="Fiber",
        last_name="Plan Customer",
        email=f"fiber-plan-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(row)
    db_session.commit()
    return row


def _technician(db_session) -> TechnicianProfile:
    user = SystemUser(
        first_name="Field",
        last_name="Planner",
        display_name="Field Planner",
        email=f"fiber-plan-tech-{uuid.uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    row = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=f"crm-plan-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _args(
    db_session,
    features: list[FiberTopologyStagedFeature],
    subscriber: Subscriber,
    technician: TechnicianProfile | None = None,
) -> dict:
    report = reconcile_fiber_field_worklist(db_session)
    return {
        "expected_worklist_report_sha256": report.report_sha256,
        "staged_feature_ids": [feature.id for feature in features],
        "subscriber_id": subscriber.id,
        "title": "Verify selected FAT evidence",
        "description": "Observe only the exact planned source scope.",
        "priority": "high",
        "address": "Jabi field route",
        "scheduled_start": datetime.now(UTC) + timedelta(hours=1),
        "scheduled_end": datetime.now(UTC) + timedelta(hours=3),
        "assigned_technician_id": technician.id if technician else None,
        "assignment_reason": "Exact fiber evidence route",
        "idempotency_key": "pytest-fiber-field-plan-0001",
    }


def test_job_plan_preview_is_exact_stable_and_write_free(db_session):
    first = _stage(db_session, "FAT-PLAN-001")
    second = _stage(db_session, "FAT-PLAN-002")
    subscriber = _subscriber(db_session)
    args = _args(db_session, [second, first], subscriber)
    before_orders = db_session.query(WorkOrder).count()

    preview = preview_fiber_field_verification_job_plan(db_session, **args)
    replay = preview_fiber_field_verification_job_plan(db_session, **args)

    assert preview["plan_sha256"] == replay["plan_sha256"]
    assert len(preview["scope_sha256"]) == 64
    assert preview["selected_feature_count"] == 2
    assert [row["staged_feature_id"] for row in preview["selected_features"]] == [
        str(first.id),
        str(second.id),
    ]
    assert all(
        row["verification_state"] == "unobserved"
        for row in preview["selected_features"]
    )
    assert preview["command"]["public_id"].startswith("sub-fv-")
    assert db_session.query(WorkOrder).count() == before_orders


def test_execute_atomically_creates_assigns_audits_and_replays(db_session):
    feature = _stage(db_session, "FAT-PLAN-EXECUTE")
    subscriber = _subscriber(db_session)
    technician = _technician(db_session)
    args = _args(db_session, [feature], subscriber, technician)
    preview = preview_fiber_field_verification_job_plan(db_session, **args)
    auth = {
        "principal_type": "system_user",
        "principal_id": str(uuid.uuid4()),
    }

    result = execute_fiber_field_verification_job_plan(
        db_session,
        expected_plan_sha256=preview["plan_sha256"],
        auth=auth,
        request_id="fiber-plan-request-1",
        **args,
    )
    replayed = execute_fiber_field_verification_job_plan(
        db_session,
        expected_plan_sha256=preview["plan_sha256"],
        auth=auth,
        request_id="fiber-plan-request-1",
        **args,
    )

    work_order = result["work_order"]
    assignment = result["assignment"]
    assert isinstance(work_order, WorkOrder)
    assert isinstance(assignment, WorkOrderAssignmentQueue)
    assert result["replayed"] is False
    assert replayed["replayed"] is True
    assert replayed["work_order"].id == work_order.id
    assert work_order.status == "dispatched"
    assert assignment.status == "assigned"
    assert assignment.assigned_technician_id == technician.id
    plan = work_order.metadata_["fiber_field_verification_plan"]
    assert plan["plan_sha256"] == preview["plan_sha256"]
    assert plan["scope_sha256"] == preview["scope_sha256"]
    assert plan["selected_features"][0]["staged_feature_id"] == str(feature.id)
    assert (
        db_session.query(WorkOrder).filter_by(public_id=work_order.public_id).count()
        == 1
    )
    assert (
        db_session.query(WorkOrderAssignmentQueue)
        .filter_by(work_order_mirror_id=work_order.id)
        .count()
        == 1
    )
    plan_events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "fiber_field_verification_job_plan.executed")
        .filter(AuditEvent.entity_id == preview["plan_sha256"])
        .all()
    )
    assert len(plan_events) == 1
    assert plan_events[0].request_id == "fiber-plan-request-1"
    assert plan_events[0].metadata_["public_id"] == work_order.public_id

    original_plan = copy.deepcopy(plan)
    work_order_commands.update_header(
        db_session,
        work_order.public_id,
        WorkOrderHeaderUpdate(
            metadata={"fiber_field_verification_plan": {"tampered": True}}
        ),
    )
    assert work_order.metadata_["fiber_field_verification_plan"] == original_plan


def test_execute_rejects_changed_confirmation_and_worklist(db_session):
    feature = _stage(db_session, "FAT-PLAN-STALE")
    subscriber = _subscriber(db_session)
    args = _args(db_session, [feature], subscriber)
    preview = preview_fiber_field_verification_job_plan(db_session, **args)

    changed = {**args, "title": "Changed after preview"}
    with pytest.raises(FiberFieldVerificationJobPlanError) as confirmation:
        execute_fiber_field_verification_job_plan(
            db_session,
            expected_plan_sha256=preview["plan_sha256"],
            **changed,
        )
    assert confirmation.value.status_code == 409
    assert "confirmation is stale" in confirmation.value.detail

    _stage(
        db_session,
        "FAT-PLAN-STALE",
        created_at=feature.created_at + timedelta(seconds=1),
    )
    with pytest.raises(FiberFieldVerificationJobPlanError) as worklist:
        execute_fiber_field_verification_job_plan(
            db_session,
            expected_plan_sha256=preview["plan_sha256"],
            **args,
        )
    assert worklist.value.status_code == 409
    assert "worklist changed" in worklist.value.detail
    assert db_session.query(WorkOrder).count() == 0


def test_planned_job_observations_fail_closed_outside_exact_scope(db_session):
    selected = _stage(db_session, "FAT-PLAN-SCOPED")
    outside = _stage(db_session, "FAT-PLAN-OUTSIDE")
    subscriber = _subscriber(db_session)
    technician = _technician(db_session)
    args = _args(db_session, [selected], subscriber, technician)
    preview = preview_fiber_field_verification_job_plan(db_session, **args)
    result = execute_fiber_field_verification_job_plan(
        db_session,
        expected_plan_sha256=preview["plan_sha256"],
        **args,
    )
    work_order = result["work_order"]

    observation = record_fiber_field_observation(
        db_session,
        staged_feature_id=selected.id,
        expected_feature_content_sha256=selected.content_sha256,
        work_order_id=work_order.id,
        recorded_by_technician_id=technician.id,
        recorded_by_person_id=technician.person_id,
        recorded_by_system_user_id=technician.system_user_id,
        verification_scope="identity",
        outcome="agrees",
        observed_at=datetime.now(UTC),
        client_ref=uuid.uuid4(),
        observed_external_label="FAT-PLAN-SCOPED",
    )
    assert isinstance(observation, FiberTopologyFieldObservation)

    with pytest.raises(FiberTopologyFieldObservationError) as exc:
        record_fiber_field_observation(
            db_session,
            staged_feature_id=outside.id,
            expected_feature_content_sha256=outside.content_sha256,
            work_order_id=work_order.id,
            recorded_by_technician_id=technician.id,
            recorded_by_person_id=technician.person_id,
            recorded_by_system_user_id=technician.system_user_id,
            verification_scope="identity",
            outcome="agrees",
            observed_at=datetime.now(UTC),
            client_ref=uuid.uuid4(),
            observed_external_label="FAT-PLAN-OUTSIDE",
        )
    assert "outside this work order's verification plan" in str(exc.value)

    tampered_metadata = copy.deepcopy(work_order.metadata_)
    tampered_metadata["fiber_field_verification_plan"]["selected_features"][0][
        "content_sha256"
    ] = _sha()
    work_order.metadata_ = tampered_metadata
    db_session.commit()

    with pytest.raises(FiberTopologyFieldObservationError) as tampered:
        record_fiber_field_observation(
            db_session,
            staged_feature_id=selected.id,
            expected_feature_content_sha256=selected.content_sha256,
            work_order_id=work_order.id,
            recorded_by_technician_id=technician.id,
            recorded_by_person_id=technician.person_id,
            recorded_by_system_user_id=technician.system_user_id,
            verification_scope="identity",
            outcome="agrees",
            observed_at=datetime.now(UTC),
            client_ref=uuid.uuid4(),
            observed_external_label="FAT-PLAN-SCOPED",
        )
    assert "source scope digest does not match" in str(tampered.value)


def test_dispatch_api_exposes_authorized_preview_and_execute(db_session):
    feature = _stage(db_session, "FAT-PLAN-API")
    subscriber = _subscriber(db_session)
    technician = _technician(db_session)
    args = _args(db_session, [feature], subscriber, technician)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: {
        "principal_type": "system_user",
        "principal_id": str(uuid.uuid4()),
        "roles": ["admin"],
        "scopes": [],
    }
    client = TestClient(app)
    payload = {
        **args,
        "staged_feature_ids": [str(feature.id)],
        "subscriber_id": str(subscriber.id),
        "assigned_technician_id": str(technician.id),
        "scheduled_start": args["scheduled_start"].isoformat(),
        "scheduled_end": args["scheduled_end"].isoformat(),
    }

    preview_response = client.post(
        "/api/v1/dispatch/field-verification-job-plans/preview",
        json=payload,
    )
    assert preview_response.status_code == 200
    plan_sha256 = preview_response.json()["plan_sha256"]
    execute_response = client.post(
        "/api/v1/dispatch/field-verification-job-plans/execute",
        headers={"X-Request-ID": "fiber-plan-api-1"},
        json={**payload, "expected_plan_sha256": plan_sha256},
    )
    assert execute_response.status_code == 201
    assert execute_response.json()["work_order"]["status"] == "dispatched"
    assert execute_response.json()["assignment"]["status"] == "assigned"
