from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.models.audit import AuditEvent
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.subscriber import Reseller, Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.network.fiber_field_verification_job_plans import (
    FiberFieldVerificationJobPlanError,
    execute_fiber_field_verification_job_plan,
    preview_fiber_field_verification_job_plan,
)
from app.services.network.fiber_topology_field_worklist import (
    reconcile_fiber_field_worklist,
)


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
    reseller = Reseller(name=f"Synthetic fiber plan reseller {uuid.uuid4()}")
    db_session.add(reseller)
    db_session.flush()
    row = Subscriber(
        reseller_id=reseller.id,
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


from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.services.network.fiber_field_verification_job_plan_contracts import (
    FiberJobPlanCommand,
    FiberJobPlanQuery,
    FiberPlanAuditPrincipal,
)
from app.services.network.fiber_field_verification_job_plans import (
    serialize_fiber_plan_command,
)
from app.services.network.fiber_field_verification_job_scope import (
    FiberFieldVerificationJobScopeError,
    assert_feature_in_planned_scope,
    planned_feature_scope,
    planned_scope_payload,
)
from app.services.owner_commands import CommandContext, OwnerCommandError
from app.services.permission_admission import PermissionAdmission
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    VendorAssignmentTarget,
)
from app.services.work_order_commands import WorkOrderCommandError


def _query(db_session, features, subscriber, target=None) -> FiberJobPlanQuery:
    report = reconcile_fiber_field_worklist(db_session)
    return FiberJobPlanQuery(
        expected_worklist_report_sha256=report.report_sha256,
        staged_feature_ids=tuple(feature.id for feature in features),
        subscriber_id=subscriber.id,
        title="Verify selected FAT evidence",
        description="Exact planned source scope.",
        priority="high",
        address="Jabi field route",
        scheduled_start=datetime.now(UTC) + timedelta(hours=1),
        scheduled_end=datetime.now(UTC) + timedelta(hours=3),
        target=target,
        assignment_reason="Exact fiber evidence route",
        idempotency_key="pytest-fiber-field-plan-0001",
    )


def _command(db_session, query, digest) -> FiberJobPlanCommand:
    staff = SystemUser(
        first_name="Dispatch",
        last_name="Staff",
        email=f"dispatch-{uuid.uuid4()}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(staff)
    db_session.flush()
    principal_id = staff.id
    return FiberJobPlanCommand(
        query=query,
        expected_plan_sha256=digest,
        context=CommandContext.system(
            actor=str(principal_id),
            scope="operations:dispatch:assign",
            reason="Exact fiber plan",
            idempotency_key=query.idempotency_key,
        ),
        access=PermissionAdmission(
            principal_id,
            "system_user",
            frozenset(
                {
                    "operations:dispatch:assign",
                    "operations:dispatch:write",
                    "network:fiber:read",
                }
            ),
        ),
        audit=FiberPlanAuditPrincipal(principal_id, "fiber-plan-request-1"),
    )


def _ready(db_session, query):
    preview = preview_fiber_field_verification_job_plan(db_session, query=query)
    command = _command(db_session, query, preview.plan_sha256)
    db_session.commit()
    return preview, command


def test_job_plan_preview_is_exact_stable_and_write_free(db_session):
    first, second = _stage(db_session, "FAT-001"), _stage(db_session, "FAT-002")
    query = _query(db_session, [second, first], _subscriber(db_session))
    before = db_session.query(WorkOrder).count()
    preview = preview_fiber_field_verification_job_plan(db_session, query=query)
    replay = preview_fiber_field_verification_job_plan(db_session, query=query)
    assert preview == replay
    assert [row.staged_feature_id for row in preview.scope.selected_features] == [
        first.id,
        second.id,
    ]
    assert all(
        row.verification_state == "unobserved"
        for row in preview.scope.selected_features
    )
    assert db_session.query(WorkOrder).count() == before
    # Exact legacy command/scope JSON is the digest input for unassigned and technician plans.
    payload = {
        "command": serialize_fiber_plan_command(preview),
        **planned_scope_payload(preview.scope),
    }
    assert "assigned_vendor_user_id" not in payload["command"]
    assert (
        hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        == preview.plan_sha256
    )


def test_execute_atomically_creates_assigns_audits_and_replays(db_session):
    feature = _stage(db_session, "FAT-EXECUTE")
    technician = _technician(db_session)
    query = _query(
        db_session,
        [feature],
        _subscriber(db_session),
        TechnicianAssignmentTarget(technician.id),
    )
    preview, command = _ready(db_session, query)
    result = execute_fiber_field_verification_job_plan(db_session, command=command)
    replay = execute_fiber_field_verification_job_plan(db_session, command=command)
    assert not result.replayed and replay.replayed
    assert result.work_order.id == replay.work_order.id
    assert result.assignment.id == replay.assignment.id
    assert (
        result.assignment_receipt.assignment_generation
        == replay.assignment_receipt.assignment_generation
    )
    assert result.work_order.status == "dispatched"
    row = db_session.get(WorkOrder, result.work_order.id)
    assert (
        row.metadata_["fiber_field_verification_plan"]["plan_sha256"]
        == preview.plan_sha256
    )
    assert planned_feature_scope(row)[0].staged_feature_id == feature.id
    assert db_session.query(WorkOrderAssignmentQueue).count() == 1
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="fiber_field_verification_job_plan.executed")
        .count()
        == 1
    )


def _vendor_target(db_session):
    native = Vendor(name="Fiber contractor", is_active=True)
    db_session.add(native)
    db_session.flush()
    vendor = FieldVendor(
        native_vendor_id=native.id, name="Fiber contractor", is_active=True
    )
    person = SystemUser(
        first_name="Vendor",
        last_name="Person",
        email=f"vendor-{uuid.uuid4()}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add_all([vendor, person])
    db_session.flush()
    member = FieldVendorUser(
        vendor_id=vendor.id, system_user_id=person.id, role="field", is_active=True
    )
    db_session.add(member)
    db_session.flush()
    return VendorAssignmentTarget(native.id, member.id), member


def test_vendor_member_changes_digest_and_revocation_denies_replay(db_session):
    feature = _stage(db_session, "FAT-VENDOR")
    target, member = _vendor_target(db_session)
    query = _query(db_session, [feature], _subscriber(db_session), target)
    other_person = SystemUser(
        first_name="Other",
        last_name="Vendor",
        email=f"other-{uuid.uuid4()}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(other_person)
    db_session.flush()
    other_member = FieldVendorUser(
        vendor_id=member.vendor_id,
        system_user_id=other_person.id,
        role="field",
        is_active=True,
    )
    db_session.add(other_member)
    db_session.flush()
    other_preview = preview_fiber_field_verification_job_plan(
        db_session,
        query=replace(
            query, target=VendorAssignmentTarget(target.vendor_id, other_member.id)
        ),
    )
    preview, command = _ready(db_session, query)
    assert serialize_fiber_plan_command(preview)["assigned_vendor_user_id"] == str(
        target.vendor_user_id
    )
    assert preview.plan_sha256 != other_preview.plan_sha256
    assert preview.scope.scope_sha256 == other_preview.scope.scope_sha256
    result = execute_fiber_field_verification_job_plan(db_session, command=command)
    assert result.assignment.assigned_vendor_id == target.vendor_id
    assert result.assignment.assigned_vendor_user_id == target.vendor_user_id
    member.is_active = False
    db_session.commit()
    with pytest.raises(WorkOrderCommandError, match="active|attending|membership"):
        execute_fiber_field_verification_job_plan(db_session, command=command)
    assert db_session.query(WorkOrder).count() == 1
    assert db_session.query(WorkOrderAssignmentQueue).count() == 1


def test_execute_rejects_changed_confirmation_and_worklist(db_session):
    feature = _stage(db_session, "FAT-STALE")
    query = _query(db_session, [feature], _subscriber(db_session))
    _, command = _ready(db_session, query)
    with pytest.raises(FiberFieldVerificationJobPlanError) as stale:
        execute_fiber_field_verification_job_plan(
            db_session, command=replace(command, query=replace(query, title="Changed"))
        )
    assert stale.value.code.endswith("stale_plan")
    _stage(
        db_session, "FAT-STALE", created_at=feature.created_at + timedelta(seconds=1)
    )
    db_session.commit()
    with pytest.raises(FiberFieldVerificationJobPlanError) as stale:
        execute_fiber_field_verification_job_plan(db_session, command=command)
    assert stale.value.code.endswith("stale_worklist")
    assert db_session.query(WorkOrder).count() == 0


def test_planned_job_observations_fail_closed_outside_exact_scope(db_session):
    selected, outside = (
        _stage(db_session, "FAT-SCOPE"),
        _stage(db_session, "FAT-OUTSIDE"),
    )
    query = _query(db_session, [selected], _subscriber(db_session))
    _, command = _ready(db_session, query)
    result = execute_fiber_field_verification_job_plan(db_session, command=command)
    row = db_session.get(WorkOrder, result.work_order.id)
    assert_feature_in_planned_scope(row, selected)
    with pytest.raises(FiberFieldVerificationJobScopeError, match="outside"):
        assert_feature_in_planned_scope(row, outside)
    metadata = copy.deepcopy(row.metadata_)
    metadata["fiber_field_verification_plan"]["selected_features"][0][
        "content_sha256"
    ] = _sha()
    row.metadata_ = metadata
    with pytest.raises(
        FiberFieldVerificationJobScopeError, match="digest does not match"
    ):
        assert_feature_in_planned_scope(row, selected)


def test_plan_rolls_back_all_participants_on_audit_failure(db_session, monkeypatch):
    feature = _stage(db_session, "FAT-ROLLBACK")
    technician = _technician(db_session)
    query = _query(
        db_session,
        [feature],
        _subscriber(db_session),
        TechnicianAssignmentTarget(technician.id),
    )
    _, command = _ready(db_session, query)

    def reject(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(
        "app.services.network.fiber_field_verification_job_plans.stage_audit_event",
        reject,
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        execute_fiber_field_verification_job_plan(db_session, command=command)
    assert db_session.query(WorkOrder).count() == 0
    assert db_session.query(WorkOrderAssignmentQueue).count() == 0


def test_missing_canonical_admission_is_denied_and_helper_commit_rolls_back(
    db_session, monkeypatch
):
    feature = _stage(db_session, "FAT-AUTH")
    query = _query(db_session, [feature], _subscriber(db_session))
    _, command = _ready(db_session, query)
    with pytest.raises(FiberFieldVerificationJobPlanError) as denied:
        execute_fiber_field_verification_job_plan(
            db_session,
            command=replace(
                command,
                access=replace(command.access, admitted_permissions=frozenset()),
            ),
        )
    assert denied.value.code.endswith("forbidden")

    def commit_inside_owner(db, **kwargs):
        db.commit()

    monkeypatch.setattr(
        "app.services.network.fiber_field_verification_job_plans.stage_audit_event",
        commit_inside_owner,
    )
    with pytest.raises(OwnerCommandError) as invalid:
        execute_fiber_field_verification_job_plan(db_session, command=command)
    assert invalid.value.code.endswith("nested_transaction_completion")
    assert db_session.query(WorkOrder).count() == 0


def test_technician_command_keeps_historical_json_shape(db_session):
    feature = _stage(db_session, "FAT-LEGACY-JSON")
    technician = _technician(db_session)
    query = _query(
        db_session,
        [feature],
        _subscriber(db_session),
        TechnicianAssignmentTarget(technician.id),
    )
    preview = preview_fiber_field_verification_job_plan(db_session, query=query)
    assert serialize_fiber_plan_command(preview) == {
        "address": query.address,
        "assigned_technician_id": str(technician.id),
        "assigned_vendor_id": None,
        "assignment_reason": query.assignment_reason,
        "description": query.description,
        "idempotency_key": query.idempotency_key,
        "priority": query.priority,
        "public_id": preview.public_id,
        "scheduled_end": query.scheduled_end.isoformat(),
        "scheduled_start": query.scheduled_start.isoformat(),
        "subscriber_id": str(query.subscriber_id),
        "title": query.title,
    }
    scope = planned_scope_payload(preview.scope)
    payload = {key: value for key, value in scope.items() if key != "scope_sha256"}
    assert (
        hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        == preview.scope.scope_sha256
    )
