"""The device-projection Celery adapter delegates one typed owner command."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.exc import OperationalError

from app.services import device_projection_reconcile as service
from app.services.db_session_adapter import db_session_adapter
from app.tasks.device_projection import reconcile_device_projections as task


def test_task_owns_session_lifecycle_and_returns_command_evidence(monkeypatch) -> None:
    fake_db = object()
    seen: dict[str, object] = {}
    reconciled_at = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
    command_id = uuid4()
    correlation_id = uuid4()

    @contextmanager
    def command_session():
        seen["session_entered"] = True
        yield fake_db
        seen["session_exited"] = True

    def reconcile(db, command):
        seen["db"] = db
        seen["command"] = command
        return service.DeviceProjectionReconcileResult(
            inserted=2,
            updated=3,
            pruned=1,
            reconciled_at=reconciled_at,
            command_id=command_id,
            correlation_id=correlation_id,
        )

    monkeypatch.setattr(
        db_session_adapter,
        "owner_command_session",
        command_session,
    )
    monkeypatch.setattr(service, "reconcile_device_projections", reconcile)

    task.push_request(id="device-projection-task-test")
    try:
        result = task.run()
    finally:
        task.pop_request()

    assert seen["session_entered"] is True
    assert seen["session_exited"] is True
    assert seen["db"] is fake_db
    command = seen["command"]
    assert isinstance(command, service.ReconcileDeviceProjectionsCommand)
    assert command.context.actor == "celery:device_projection_reconcile"
    assert command.context.scope == "network:global"
    assert command.context.idempotency_key == "celery:device-projection-task-test"
    assert command.context.command_id == uuid5(
        NAMESPACE_URL,
        "dotmac:device-projection-reconcile:device-projection-task-test",
    )
    assert OperationalError in task.autoretry_for
    assert result == {
        "inserted": 2,
        "updated": 3,
        "pruned": 1,
        "total": 5,
        "reconciled_at": reconciled_at.isoformat(),
        "command_id": str(command_id),
        "correlation_id": str(correlation_id),
    }
