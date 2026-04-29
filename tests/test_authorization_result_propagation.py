from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.models.network_operation import NetworkOperation, NetworkOperationStatus
from app.services.network import olt_api_operations
from app.services.network.authorization_executor import AuthorizationExecutionResult
from app.services.network.result_adapter import ResultStatus
from app.tasks import ont_authorization as ont_authorization_tasks


def test_queue_authorize_ont_marks_operation_failed_when_dispatch_fails(
    db_session, monkeypatch
):
    """API authorization must not report queued when the task dispatch fails."""

    def fake_enqueue_task(*args, **kwargs):
        return SimpleNamespace(
            queued=False,
            task_id=None,
            error="broker unavailable",
        )

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    olt_id = str(uuid4())
    result = olt_api_operations.queue_authorize_ont(
        db_session,
        olt_id,
        fsp="0/1/1",
        serial_number="HWTCQUEUEFAIL",
        force_reauthorize=True,
    )

    assert result.success is False
    assert result.message == "Authorization was not queued: broker unavailable"
    assert result.data is not None
    assert result.data["status"] == "queue_failed"

    operation = db_session.scalars(
        select(NetworkOperation).where(
            NetworkOperation.correlation_key
            == f"olt-authorize:{olt_id}:0/1/1:HWTCQUEUEFAIL"
        )
    ).one()
    assert operation.status == NetworkOperationStatus.failed
    assert operation.error == "broker unavailable"
    assert operation.output_payload == {
        "status": "queue_failed",
        "task_name": "app.tasks.ont_authorization.authorize_ont_from_olt_api",
        "error": "broker unavailable",
    }


def test_authorize_ont_task_marks_warning_result_as_warning_operation(monkeypatch):
    """Queued authorization warnings persist as degraded success, not failure."""
    calls: list[tuple] = []

    class FakeDb:
        def commit(self):
            calls.append(("commit",))

        def rollback(self):
            calls.append(("rollback",))

        def close(self):
            calls.append(("close",))

    class FakeNetworkOperations:
        def mark_running(self, db, operation_id):
            calls.append(("running", operation_id))

        def mark_succeeded(self, db, operation_id, *, output_payload=None):
            calls.append(("succeeded", operation_id, output_payload))

        def mark_failed(self, db, operation_id, error, *, output_payload=None):
            calls.append(("failed", operation_id, error, output_payload))

        def mark_warning(self, db, operation_id, warning, *, output_payload=None):
            calls.append(("warning", operation_id, warning, output_payload))

    warning_result = SimpleNamespace(
        success=True,
        status="warning",
        message="ONT authorized, but post-authorization ACS follow-up was not queued.",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
        follow_up_operation_id=None,
        pending_rediscovery=False,
        rediscovery_task_id=None,
        steps=[],
    )

    monkeypatch.setattr(
        ont_authorization_tasks.db_session_adapter,
        "create_session",
        lambda: FakeDb(),
    )
    monkeypatch.setattr(
        "app.services.network.ont_authorization.authorize_autofind_ont_and_provision_network_audited",
        lambda *args, **kwargs: warning_result,
    )
    monkeypatch.setattr(
        "app.services.network_operations.network_operations",
        FakeNetworkOperations(),
    )

    response = ont_authorization_tasks.authorize_ont_from_olt_api(
        "op-1",
        "olt-1",
        "0/1/1",
        "HWTCWARNQUEUE",
    )

    assert response["success"] is True
    assert response["data"]["status"] == "warning"
    assert not any(call[0] in ("succeeded", "failed") for call in calls)
    warning_calls = [call for call in calls if call[0] == "warning"]
    assert len(warning_calls) == 1
    assert warning_calls[0][2] == (
        "ONT authorized, but post-authorization ACS follow-up was not queued."
    )
    assert warning_calls[0][3]["status"] == "warning"


def test_authorization_execution_result_renders_warning():
    """Sync authorization callers preserve warning status for UI rendering."""
    result = AuthorizationExecutionResult(
        success=True,
        message="ONT authorized, but post-authorization ACS follow-up was not queued.",
        ont_id="ont-1",
        serial_number="HWTCWARNEXEC",
        fsp="0/1/1",
        details={"status": "warning", "ont_id_on_olt": 7},
    )

    operation_result = result.to_operation_result()

    assert operation_result.status == ResultStatus.warning
    assert operation_result.title == "Authorization Warning"
    assert operation_result.data == {
        "ont_id": "ont-1",
        "serial_number": "HWTCWARNEXEC",
        "fsp": "0/1/1",
        "status": "warning",
        "ont_id_on_olt": 7,
    }
