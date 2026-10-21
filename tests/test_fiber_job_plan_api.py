"""Dispatch adapters preserve the confirmed typed fiber plan and public wire shape."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import dispatch as api
from app.schemas.dispatch import (
    FiberFieldVerificationJobPlanExecuteRead,
    FiberFieldVerificationJobPlanExecuteRequest,
    FiberFieldVerificationJobPlanPreviewRead,
    FiberFieldVerificationJobPlanRequest,
)
from app.services.permission_admission import PermissionAdmission
from tests.test_fiber_field_verification_job_plans import (
    _command,
    _query,
    _stage,
    _subscriber,
    _vendor_target,
)


def test_api_preview_execute_and_replay_keep_exact_attending_person(db_session):
    feature = _stage(db_session, "adapter-selected-source")
    customer = _subscriber(db_session)
    target, _member = _vendor_target(db_session)
    query = _query(db_session, [feature], customer, target)
    seed = _command(db_session, query, "0" * 64)
    db_session.commit()
    payload = FiberFieldVerificationJobPlanRequest(
        expected_worklist_report_sha256=query.expected_worklist_report_sha256,
        staged_feature_ids=list(query.staged_feature_ids),
        subscriber_id=query.subscriber_id,
        title=query.title,
        description=query.description,
        priority=query.priority,
        address=query.address,
        scheduled_start=query.scheduled_start,
        scheduled_end=query.scheduled_end,
        assigned_vendor_id=target.vendor_id,
        assigned_vendor_user_id=target.vendor_user_id,
        assignment_reason=query.assignment_reason,
        idempotency_key=query.idempotency_key,
    )
    preview = FiberFieldVerificationJobPlanPreviewRead.model_validate(
        api.preview_field_verification_job_plan(
            payload=payload,
            auth={},
            write_auth={},
            network_auth={},
            db=db_session,
        )
    )
    assert preview.command.assigned_vendor_user_id == target.vendor_user_id
    execute = FiberFieldVerificationJobPlanExecuteRequest(
        **payload.model_dump(), expected_plan_sha256=preview.plan_sha256
    )

    def run():
        return FiberFieldVerificationJobPlanExecuteRead.model_validate(
            api.execute_field_verification_job_plan(
                payload=execute,
                access=seed.access,
                write_access=seed.access,
                network_access=seed.access,
                request_id="fiber-adapter-test",
                db=db_session,
            )
        )

    created = run()
    replay = run()
    assert not created.replayed and replay.replayed
    assert created.work_order.id == replay.work_order.id
    assert created.work_order.title == query.title
    assert created.assignment.assigned_vendor_user_id == target.vendor_user_id
    assert replay.assignment.id == created.assignment.id
    assert created.plan.plan_sha256 == preview.plan_sha256


def test_execute_rejects_permission_proofs_from_different_principals():
    payload = FiberFieldVerificationJobPlanExecuteRequest(
        expected_worklist_report_sha256="0" * 64,
        expected_plan_sha256="1" * 64,
        staged_feature_ids=[uuid4()],
        subscriber_id=uuid4(),
        title="Synthetic job",
        idempotency_key="adapter-proof-rejection",
    )
    staff = PermissionAdmission(
        uuid4(), "system_user", frozenset({"operations:dispatch:assign"})
    )
    other = PermissionAdmission(
        uuid4(), "system_user", frozenset({"operations:dispatch:write"})
    )
    with pytest.raises(HTTPException) as exc:
        api.execute_field_verification_job_plan(
            payload=payload,
            access=staff,
            write_access=other,
            network_access=staff,
            request_id=None,
            db=None,
        )
    assert exc.value.status_code == 403
