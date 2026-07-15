from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import update

from app.models.network import OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationDispatchStatus,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import network_operation_dispatch as dispatch_service
from app.services import network_operation_recovery as recovery
from app.services.network_operation_recovery import (
    NetworkOperationRecoveryError,
    RedriveOutcome,
)
from app.services.network_operations import network_operations
from app.services.queue_adapter import QueueDispatchResult
from app.services.web_network_operations import build_operation_history
from app.web.admin import network_operations as admin_network_operations


def _ont(db_session, serial: str = "REDRIVE-ONT") -> OntUnit:
    ont = OntUnit(serial_number=serial, is_active=True)
    db_session.add(ont)
    db_session.commit()
    return ont


def _failed_refresh(
    db_session,
    ont: OntUnit,
    *,
    retry_count: int = 0,
    max_retries: int = 3,
) -> NetworkOperation:
    operation = network_operations.start(
        db_session,
        NetworkOperationType.olt_ont_sync,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"ont_status_refresh:{ont.id}",
        input_payload={"action": "status_refresh"},
        initiated_by="operator",
    )
    operation.retry_count = retry_count
    operation.max_retries = max_retries
    network_operations.mark_running(db_session, str(operation.id))
    network_operations.mark_failed(db_session, str(operation.id), "OLT timeout")
    db_session.commit()
    return operation


def test_redrive_creates_new_attempt_and_keeps_failed_source_immutable(
    db_session,
):
    ont = _ont(db_session)
    source = _failed_refresh(db_session, ont)
    source_error = source.error
    source_completed_at = source.completed_at
    review = recovery.review_redrive(db_session, source)
    result = recovery.redrive_operation(
        db_session,
        str(source.id),
        expected_head=str(review.expected_head),
        idempotency_key="retry-request-0001",
        reason="OLT connectivity has been restored",
        initiated_by="Network Admin",
    )

    db_session.refresh(source)
    attempt = result.operation
    assert result.outcome == RedriveOutcome.queued
    assert result.replayed is False
    assert source.status == NetworkOperationStatus.failed
    assert source.error == source_error
    assert source.completed_at == source_completed_at
    assert attempt.id != source.id
    assert attempt.redrive_of_id == source.id
    assert attempt.parent_id is None
    assert attempt.status == NetworkOperationStatus.pending
    assert attempt.retry_count == 1
    assert attempt.max_retries == 3
    assert attempt.redrive_reviewed_head == review.expected_head
    assert attempt.redrive_reason == "OLT connectivity has been restored"
    assert attempt.input_payload == {
        "action": "status_refresh",
        "_redrive": {
            "source_operation_id": str(source.id),
            "reason": "OLT connectivity has been restored",
            "reviewed_head": review.expected_head,
        },
    }
    dispatch = db_session.query(NetworkOperationDispatch).one()
    assert dispatch.operation_id == attempt.id
    assert dispatch.command_name == "ont_status_refresh.v1"
    assert dispatch.task_name == (
        "app.tasks.ont_runtime_status.refresh_single_ont_status"
    )
    assert dispatch.args_payload == [str(ont.id), str(attempt.id)]
    assert dispatch.status == NetworkOperationDispatchStatus.pending


def test_same_redrive_request_replays_without_second_dispatch(db_session):
    ont = _ont(db_session)
    source = _failed_refresh(db_session, ont)
    review = recovery.review_redrive(db_session, source)
    kwargs = {
        "expected_head": str(review.expected_head),
        "idempotency_key": "retry-request-0002",
        "reason": "Retry after restoring OLT access",
        "initiated_by": "Network Admin",
    }

    first = recovery.redrive_operation(db_session, str(source.id), **kwargs)
    second = recovery.redrive_operation(db_session, str(source.id), **kwargs)

    assert second.outcome == RedriveOutcome.replayed
    assert second.replayed is True
    assert second.operation.id == first.operation.id
    attempts = (
        db_session.query(NetworkOperation).filter_by(redrive_of_id=source.id).all()
    )
    assert [attempt.id for attempt in attempts] == [first.operation.id]
    assert db_session.query(NetworkOperationDispatch).count() == 1


def test_redrive_rejects_stale_target_review(db_session):
    ont = _ont(db_session)
    source = _failed_refresh(db_session, ont)
    review = recovery.review_redrive(db_session, source)
    ont.serial_number = "REDRIVE-ONT-CHANGED"
    db_session.commit()
    with pytest.raises(NetworkOperationRecoveryError) as caught:
        recovery.redrive_operation(
            db_session,
            str(source.id),
            expected_head=str(review.expected_head),
            idempotency_key="retry-request-0003",
            reason="Retry after restoring OLT access",
            initiated_by="Network Admin",
        )

    assert caught.value.code == "stale_review"
    assert (
        db_session.query(NetworkOperation).filter_by(redrive_of_id=source.id).count()
        == 0
    )


def test_redrive_locked_read_refreshes_stale_source_status(db_session):
    ont = _ont(db_session)
    source = _failed_refresh(db_session, ont)
    review = recovery.review_redrive(db_session, source)
    source_id = source.id
    assert source.status == NetworkOperationStatus.failed

    db_session.execute(
        update(NetworkOperation)
        .where(NetworkOperation.id == source_id)
        .values(status=NetworkOperationStatus.succeeded)
        .execution_options(synchronize_session=False)
    )
    db_session.flush()
    assert source.status == NetworkOperationStatus.failed
    with pytest.raises(NetworkOperationRecoveryError) as caught:
        recovery.redrive_operation(
            db_session,
            str(source_id),
            expected_head=str(review.expected_head),
            idempotency_key="retry-request-stale-source",
            reason="Retry from a now stale review",
            initiated_by="Network Admin",
        )

    assert caught.value.code == "source_not_failed"
    assert source.status == NetworkOperationStatus.succeeded


def test_redrive_fails_closed_for_unregistered_device_write(db_session):
    ont = _ont(db_session)
    operation = network_operations.start(
        db_session,
        NetworkOperationType.ont_firmware_upgrade,
        NetworkOperationTargetType.ont,
        str(ont.id),
        input_payload={"artifact_id": "firmware-1"},
    )
    network_operations.mark_running(db_session, str(operation.id))
    network_operations.mark_failed(db_session, str(operation.id), "upgrade failed")
    db_session.commit()

    review = recovery.review_redrive(db_session, operation)

    assert review.eligible is False
    assert review.code == "unsupported_operation"
    assert review.expected_head is None


def test_redrive_rejects_exhausted_attempt_limit(db_session):
    ont = _ont(db_session)
    operation = _failed_refresh(db_session, ont, retry_count=3, max_retries=3)

    review = recovery.review_redrive(db_session, operation)

    assert review.eligible is False
    assert review.code == "retry_limit_reached"


def test_dispatch_failure_is_recorded_on_new_attempt(db_session, monkeypatch):
    ont = _ont(db_session)
    source = _failed_refresh(db_session, ont)
    review = recovery.review_redrive(db_session, source)
    result = recovery.redrive_operation(
        db_session,
        str(source.id),
        expected_head=str(review.expected_head),
        idempotency_key="retry-request-0004",
        reason="Retry after restoring broker access",
        initiated_by="Network Admin",
    )
    dispatch = db_session.query(NetworkOperationDispatch).one()
    dispatch.max_attempts = 1
    db_session.commit()
    monkeypatch.setattr(
        dispatch_service,
        "enqueue_task",
        lambda *_args, **_kwargs: QueueDispatchResult(
            queued=False,
            task_name="app.tasks.ont_runtime_status.refresh_single_ont_status",
            error="broker unavailable",
        ),
    )

    dispatch_service.publish_ready_dispatches(db_session)

    db_session.refresh(source)
    db_session.refresh(result.operation)
    db_session.refresh(dispatch)
    assert result.operation.status == NetworkOperationStatus.failed
    assert "retry budget" in str(result.operation.error)
    assert dispatch.status == NetworkOperationDispatchStatus.failed
    assert source.status == NetworkOperationStatus.failed


def test_history_projects_only_current_eligible_attempt(db_session):
    ont = _ont(db_session)
    source = _failed_refresh(db_session, ont)
    history = build_operation_history(db_session, "ont", str(ont.id))
    assert history[0]["can_retry"] is True
    assert history[0]["redrive_expected_head"]
    assert history[0]["redrive_idempotency_key"]

    review = recovery.review_redrive(db_session, source)
    result = recovery.redrive_operation(
        db_session,
        str(source.id),
        expected_head=str(review.expected_head),
        idempotency_key="retry-request-0005",
        reason="Retry after restoring OLT access",
        initiated_by="Network Admin",
    )
    network_operations.mark_running(db_session, str(result.operation.id))
    network_operations.mark_failed(
        db_session, str(result.operation.id), "second timeout"
    )
    db_session.commit()

    history = build_operation_history(db_session, "ont", str(ont.id))
    by_id = {entry["id"]: entry for entry in history}
    assert by_id[str(source.id)]["can_retry"] is False
    assert (
        by_id[str(source.id)]["retry_reason"] == "A newer retry attempt already exists."
    )
    assert by_id[str(result.operation.id)]["can_retry"] is True
    assert by_id[str(result.operation.id)]["retry_count"] == 1


def test_redrive_route_and_template_enforce_operator_contract():
    route = next(
        route
        for route in admin_network_operations.router.routes
        if isinstance(route, APIRoute)
        and route.path == "/network/operations/{operation_id}/redrive"
    )
    closure_values = [
        cell.cell_contents
        for dependency in route.dependant.dependencies
        for cell in (getattr(dependency.call, "__closure__", None) or ())
    ]
    assert recovery.REDRIVE_PERMISSION in closure_values

    template = Path("templates/admin/network/_operations_history.html").read_text()
    assert "can_redrive_network_operations and op.can_retry" in template
    assert 'name="expected_head"' in template
    assert 'name="idempotency_key"' in template
    assert 'name="reason"' in template


def test_redrive_permission_is_admin_only_in_seed_catalog():
    from scripts.seed import seed_rbac

    permission_keys = {key for key, _description in seed_rbac.DEFAULT_PERMISSIONS}
    assert recovery.REDRIVE_PERMISSION in permission_keys
    assert recovery.REDRIVE_PERMISSION in seed_rbac.ADMIN_ONLY_PERMISSION_KEYS
    assert recovery.REDRIVE_PERMISSION in seed_rbac.ROLE_PERMISSIONS["admin"]
    assert recovery.REDRIVE_PERMISSION not in seed_rbac.ROLE_PERMISSIONS["operator"]


def test_operation_health_snapshot_separates_redrive_outcomes(db_session):
    ont = _ont(db_session)
    source = _failed_refresh(db_session, ont)
    review = recovery.review_redrive(db_session, source)
    result = recovery.redrive_operation(
        db_session,
        str(source.id),
        expected_head=str(review.expected_head),
        idempotency_key="retry-request-0006",
        reason="Retry after restoring OLT access",
        initiated_by="Network Admin",
    )
    network_operations.mark_running(db_session, str(result.operation.id))
    network_operations.mark_succeeded(db_session, str(result.operation.id))
    db_session.commit()

    snapshot = network_operations.health_snapshot(db_session)

    assert snapshot["operations_failed"] >= 1
    assert snapshot["operations_succeeded"] >= 1
    assert snapshot["redrives_succeeded"] == 1
    assert snapshot["active"] == 0
