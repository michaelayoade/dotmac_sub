from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network_operations import network_operations
from app.tasks.ont_provisioning import (
    _queue_post_authorization_bootstrap_follow_up,
)


def test_queue_post_authorization_bootstrap_follow_up_creates_child_operation(
    db_session, monkeypatch
):
    queued_calls: list[tuple[str, dict[str, object]]] = []

    def fake_enqueue_task(task_name: str, **kwargs: object) -> SimpleNamespace:
        queued_calls.append((task_name, kwargs))
        return SimpleNamespace(queued=True, task_id="task-123")

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    parent = network_operations.start(
        db_session,
        NetworkOperationType.ont_authorize,
        NetworkOperationTargetType.ont,
        str(uuid.uuid4()),
        correlation_key=f"ont_authorize:test:{uuid.uuid4()}",
        initiated_by="tester",
    )
    network_operations.mark_running(db_session, str(parent.id))

    ont_id = str(uuid.uuid4())
    result = _queue_post_authorization_bootstrap_follow_up(
        db_session,
        ont_id=ont_id,
        parent_operation_id=str(parent.id),
        initiated_by="tester",
    )

    db_session.expire_all()
    child = db_session.scalars(
        select(NetworkOperation).where(
            NetworkOperation.correlation_key == f"tr069_bootstrap:{ont_id}"
        )
    ).one()

    assert result == {
        "queued": True,
        "operation_id": str(child.id),
        "task_id": "task-123",
        "duplicate": False,
        "error": None,
    }
    assert child.operation_type == NetworkOperationType.tr069_bootstrap
    assert child.target_type == NetworkOperationTargetType.ont
    assert str(child.target_id) == ont_id
    assert str(child.parent_id) == str(parent.id)
    assert child.initiated_by == "tester"
    assert queued_calls == [
        (
            "app.tasks.tr069.wait_for_ont_bootstrap",
            {
                "args": [ont_id, str(child.id), 0],
                "correlation_id": f"tr069_bootstrap:{ont_id}",
                "source": "ont_authorization_follow_up",
            },
        )
    ]
