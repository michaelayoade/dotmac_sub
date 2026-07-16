from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from app.models.network import OntUnit
from app.models.network_operation import (
    NetworkOperationDispatch,
    NetworkOperationDispatchStatus,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import network_operation_dispatch as dispatch_owner
from app.services.network_operation_dispatch import NetworkOperationCommand
from app.services.network_operations import network_operations
from app.services.queue_adapter import QueueDispatchResult
from app.tasks.olt_firmware import upgrade_firmware_task
from app.tasks.ont_firmware import apply_huawei_ont_firmware
from app.tasks.ont_provisioning import authorize_ont, provision_ont
from app.tasks.ont_reconcile import reconcile_huawei_ont
from app.tasks.ont_runtime_status import refresh_single_ont_status
from app.tasks.tr069 import wait_for_ont_bootstrap


def _staged_refresh(db_session, *, max_attempts: int = 5):
    ont = OntUnit(serial_number="DISPATCH-ONT", is_active=True)
    db_session.add(ont)
    db_session.flush()
    operation = network_operations.start(
        db_session,
        NetworkOperationType.olt_ont_sync,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"ont_status_refresh:{ont.id}",
        input_payload={"action": "status_refresh"},
        initiated_by="Network Admin",
    )
    dispatch = dispatch_owner.stage_dispatch(
        db_session,
        operation,
        NetworkOperationCommand.ont_status_refresh_v1,
        max_attempts=max_attempts,
    )
    db_session.commit()
    return ont, operation, dispatch


def test_stage_dispatch_is_typed_and_idempotent(db_session):
    ont, operation, dispatch = _staged_refresh(db_session)

    replay = dispatch_owner.stage_dispatch(
        db_session,
        operation,
        NetworkOperationCommand.ont_status_refresh_v1,
    )

    assert replay.id == dispatch.id
    assert db_session.query(NetworkOperationDispatch).count() == 1
    assert dispatch.command_name == "ont_status_refresh.v1"
    assert dispatch.task_name == (
        "app.tasks.ont_runtime_status.refresh_single_ont_status"
    )
    assert dispatch.args_payload == [str(ont.id), str(operation.id)]
    assert dispatch.kwargs_payload == {}
    assert dispatch.queue == "ingestion"


def test_stage_dispatch_rejects_arbitrary_or_mismatched_commands(db_session):
    _, operation, _ = _staged_refresh(db_session)

    with pytest.raises(dispatch_owner.NetworkOperationDispatchError) as arbitrary:
        dispatch_owner.stage_dispatch(db_session, operation, "arbitrary.task")
    with pytest.raises(dispatch_owner.NetworkOperationDispatchError) as mismatch:
        dispatch_owner.stage_dispatch(
            db_session,
            operation,
            NetworkOperationCommand.ont_firmware_upgrade_v1,
            dispatch_key="secondary",
        )

    assert arbitrary.value.code == "unsupported_command"
    assert mismatch.value.code == "operation_command_mismatch"


@pytest.mark.parametrize(
    ("task", "args", "kwargs"),
    [
        (refresh_single_ont_status, ("ont-1", "operation-1"), {}),
        (
            apply_huawei_ont_firmware,
            ("ont-1", "image-1", "operation-1"),
            {},
        ),
        (
            upgrade_firmware_task,
            ("olt-1", "image-1"),
            {"operation_id": "operation-1"},
        ),
        (reconcile_huawei_ont, ("ont-1", "operation-1"), {}),
        (
            authorize_ont,
            ("olt-1", "0/1/0", "HWTSERIAL"),
            {"operation_id": "operation-1"},
        ),
        (
            provision_ont,
            ("ont-1",),
            {"operation_id": "operation-1"},
        ),
        (
            wait_for_ont_bootstrap,
            ("ont-1", "operation-1", 0),
            {},
        ),
    ],
)
def test_managed_celery_signatures_accept_dispatch_claim(task, args, kwargs):
    task.__header__(
        *args,
        **kwargs,
        _network_dispatch_id="dispatch-1",
    )


def test_publisher_sends_only_the_dispatch_envelope(db_session, monkeypatch):
    _, operation, dispatch = _staged_refresh(db_session)
    calls: list[tuple[str, dict[str, object]]] = []

    def _enqueue(task_name: str, **kwargs):
        calls.append((task_name, kwargs))
        return QueueDispatchResult(
            queued=True,
            task_id="envelope-task-1",
            task_name=task_name,
            queue=kwargs.get("queue"),
        )

    monkeypatch.setattr(dispatch_owner, "enqueue_task", _enqueue)
    result = dispatch_owner.publish_ready_dispatches(db_session)

    db_session.refresh(dispatch)
    assert result.dispatched == 1
    assert dispatch.status == NetworkOperationDispatchStatus.dispatched
    assert dispatch.task_id == "envelope-task-1"
    assert calls == [
        (
            dispatch.task_name,
            {
                "args": list(dispatch.args_payload),
                "kwargs": {"_network_dispatch_id": str(dispatch.id)},
                "queue": "ingestion",
                "correlation_id": operation.correlation_key,
                "source": "network_operation_dispatch",
                "request_id": str(dispatch.id),
                "actor_id": "Network Admin",
                "headers": {
                    "network_operation_id": str(operation.id),
                    "network_command": "ont_status_refresh.v1",
                },
            },
        )
    ]


def test_dispatch_claim_allows_only_one_envelope_execution(db_session):
    _, _, dispatch = _staged_refresh(db_session)

    first = dispatch_owner.claim_dispatch_execution(db_session, str(dispatch.id))
    db_session.commit()
    second = dispatch_owner.claim_dispatch_execution(db_session, str(dispatch.id))

    assert first is not None
    assert first.task_name == ("app.tasks.ont_runtime_status.refresh_single_ont_status")
    assert second is None
    db_session.refresh(dispatch)
    assert dispatch.status == NetworkOperationDispatchStatus.acknowledged

    dispatch_owner.complete_dispatch_execution(db_session, str(dispatch.id))
    db_session.commit()
    assert dispatch.status == NetworkOperationDispatchStatus.completed


def test_executor_invokes_registered_target_once(db_session, monkeypatch):
    _, _, dispatch = _staged_refresh(db_session)
    run = Mock(return_value={"success": True})

    @contextmanager
    def _session():
        yield db_session

    monkeypatch.setattr(
        "app.services.db_session_adapter.db_session_adapter.session",
        _session,
    )
    target = dispatch_owner.managed_network_operation_dispatch(dispatch.task_name)(run)

    first = target(
        *dispatch.args_payload,
        **dispatch.kwargs_payload,
        _network_dispatch_id=str(dispatch.id),
    )
    second = target(
        *dispatch.args_payload,
        **dispatch.kwargs_payload,
        _network_dispatch_id=str(dispatch.id),
    )

    run.assert_called_once_with(
        *dispatch.args_payload,
        **dispatch.kwargs_payload,
        _network_dispatch_id=str(dispatch.id),
    )
    assert first == {"success": True}
    assert second == {
        "dispatch_id": str(dispatch.id),
        "executed": False,
        "duplicate": True,
    }
    db_session.refresh(dispatch)
    assert dispatch.status == NetworkOperationDispatchStatus.completed


def test_publish_failure_exhaustion_fails_operation_without_device_execution(
    db_session, monkeypatch
):
    _, operation, dispatch = _staged_refresh(db_session, max_attempts=2)
    current = datetime.now(UTC)
    monkeypatch.setattr(
        dispatch_owner,
        "enqueue_task",
        lambda *_args, **_kwargs: QueueDispatchResult(
            queued=False,
            task_name=dispatch.task_name,
            error="broker unavailable",
        ),
    )

    first = dispatch_owner.publish_ready_dispatches(db_session, now=current)
    second = dispatch_owner.publish_ready_dispatches(
        db_session,
        now=current + timedelta(seconds=3),
    )

    db_session.refresh(dispatch)
    db_session.refresh(operation)
    assert first.retried == 1
    assert second.failed == 1
    assert dispatch.attempts == 2
    assert dispatch.status == NetworkOperationDispatchStatus.failed
    assert operation.status == NetworkOperationStatus.failed
    assert "retry budget" in str(operation.error)


def test_unacknowledged_broker_delivery_fails_closed_after_budget(
    db_session, monkeypatch
):
    _, operation, dispatch = _staged_refresh(db_session, max_attempts=1)
    current = datetime.now(UTC)
    monkeypatch.setattr(
        dispatch_owner,
        "enqueue_task",
        lambda task_name, **kwargs: QueueDispatchResult(
            queued=True,
            task_id="unacknowledged-envelope",
            task_name=task_name,
            queue=kwargs.get("queue"),
        ),
    )
    dispatch_owner.publish_ready_dispatches(db_session, now=current)

    result = dispatch_owner.publish_ready_dispatches(
        db_session,
        now=current + dispatch_owner.DEFAULT_REDELIVERY_AFTER + timedelta(seconds=1),
    )

    db_session.refresh(dispatch)
    db_session.refresh(operation)
    assert result.reconciliation_needed == 1
    assert dispatch.status == NetworkOperationDispatchStatus.reconciliation_needed
    assert operation.status == NetworkOperationStatus.failed
    assert dispatch_owner.claim_dispatch_execution(db_session, str(dispatch.id)) is None


def test_stale_worker_acknowledgement_requires_current_state_review(db_session):
    _, operation, dispatch = _staged_refresh(db_session)
    current = datetime.now(UTC)
    claim = dispatch_owner.claim_dispatch_execution(
        db_session,
        str(dispatch.id),
        now=current,
    )
    db_session.commit()
    assert claim is not None

    result = dispatch_owner.reconcile_dispatches(
        db_session,
        now=current + dispatch_owner.DEFAULT_EXECUTION_TIMEOUT + timedelta(seconds=1),
    )

    db_session.refresh(dispatch)
    db_session.refresh(operation)
    assert result.reconciliation_needed == 1
    assert dispatch.status == NetworkOperationDispatchStatus.reconciliation_needed
    assert operation.status == NetworkOperationStatus.failed


def test_claim_rejects_tampered_stored_command(db_session):
    _, operation, dispatch = _staged_refresh(db_session)
    dispatch.args_payload = ["different-target", str(operation.id)]
    db_session.commit()

    claim = dispatch_owner.claim_dispatch_execution(db_session, str(dispatch.id))
    db_session.commit()

    db_session.refresh(dispatch)
    db_session.refresh(operation)
    assert claim is None
    assert dispatch.status == NetworkOperationDispatchStatus.reconciliation_needed
    assert operation.status == NetworkOperationStatus.failed


def test_dispatch_health_snapshot_initializes_all_states(db_session):
    _staged_refresh(db_session)

    snapshot = dispatch_owner.health_snapshot(db_session)

    assert snapshot["dispatches_pending"] == 1
    assert snapshot["dispatches_reconciliation_needed"] == 0
    assert snapshot["dispatch_oldest_pending_age_seconds"] >= 0
