from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.network.ont_reconcile_queue import queue_olt_acs_reconciliation
from app.services.network.reconcile.core import _resolve_acs_client
from app.services.network.reconcile.state import (
    AppliedAction,
    Drift,
    ReconcileResult,
)
from app.services.queue_adapter import QueueDispatchResult
from app.tasks.ont_reconcile import _reconcile_payload, reconcile_huawei_ont


def test_queue_olt_acs_reconciliation_tracks_children_before_dispatch() -> None:
    olt = SimpleNamespace(id=uuid.uuid4(), tr069_acs_server_id=uuid.uuid4())
    ont = SimpleNamespace(id=uuid.uuid4())
    db = MagicMock()
    db.scalars.return_value.all.return_value = [ont]
    parent = SimpleNamespace(id=uuid.uuid4())
    child = SimpleNamespace(id=uuid.uuid4())

    with (
        patch(
            "app.services.network.ont_reconcile_queue.network_operations.start",
            side_effect=[parent, child],
        ) as start,
        patch(
            "app.services.network.ont_reconcile_queue.network_operations.update_parent_status"
        ) as update_parent,
        patch(
            "app.services.network.ont_reconcile_queue.enqueue_task",
            return_value=QueueDispatchResult(queued=True, task_id="task-1"),
        ) as enqueue,
    ):
        result = queue_olt_acs_reconciliation(db, olt)

    assert result == {
        "attempted": 1,
        "queued": 1,
        "duplicates": 0,
        "errors": 0,
        "operation_id": str(parent.id),
    }
    assert start.call_count == 2
    assert db.commit.call_count == 2
    enqueue.assert_called_once_with(
        "app.tasks.ont_reconcile.reconcile_huawei_ont",
        args=[str(ont.id), str(child.id)],
        correlation_id=f"ont_desired_reconcile:{ont.id}",
        source="olt_acs_assignment",
    )
    update_parent.assert_called_once_with(db, str(parent.id))


def test_reconcile_operation_payload_never_records_secret_values() -> None:
    result = ReconcileResult(
        success=True,
        sync_status="synced",
        actions_applied=(
            AppliedAction(
                field="wifi_password",
                surface="acs",
                old_value="old-secret",
                new_value="new-secret",
                duration_ms=12,
            ),
        ),
        drift_before=(
            Drift(
                field="wifi_password",
                surface="acs",
                desired="new-secret",
                observed="old-secret",
                repairable=True,
            ),
        ),
        drift_after=(),
        observed_after=None,
        failure=None,
        duration_ms=25,
        reconciled_at=datetime.now(UTC),
    )

    payload = _reconcile_payload(result)

    assert payload["actions"] == [
        {"field": "wifi_password", "surface": "acs", "duration_ms": 12}
    ]
    assert payload["drift_before"] == ["wifi_password"]
    assert "old-secret" not in repr(payload)
    assert "new-secret" not in repr(payload)


def test_reconcile_operation_payload_persists_classifier_evidence() -> None:
    evidence = {
        "error_code": "unknown_command",
        "huawei_cli_response": {
            "response_code": "unknown_command",
            "unsupported": True,
        },
    }
    result = ReconcileResult(
        success=True,
        sync_status="synced",
        actions_applied=(
            AppliedAction(
                field="olt_description",
                surface="olt",
                old_value="old",
                new_value="new",
                duration_ms=12,
                evidence=evidence,
            ),
        ),
        drift_before=(),
        drift_after=(),
        observed_after=None,
        failure=None,
        duration_ms=25,
        reconciled_at=datetime.now(UTC),
    )

    payload = _reconcile_payload(result)

    assert payload["actions"][0]["evidence"] == evidence


def test_acs_migration_uses_current_link_as_write_transport() -> None:
    ont = SimpleNamespace(id=uuid.uuid4())
    linked = SimpleNamespace(acs_server_id=uuid.uuid4())
    observed_server = SimpleNamespace(base_url="http://old-acs:7557")
    db = MagicMock()
    db.scalars.return_value.first.return_value = linked
    db.get.return_value = observed_server
    client = object()

    with patch(
        "app.services.genieacs_client.create_genieacs_client",
        return_value=client,
    ) as create_client:
        resolved = _resolve_acs_client(db, ont)

    assert resolved is client
    create_client.assert_called_once_with("http://old-acs:7557")


def test_targeted_task_forces_acs_credentials_then_aligns_observed_link() -> None:
    ont_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    ont = SimpleNamespace(id=uuid.UUID(ont_id))
    acs = SimpleNamespace(
        id=uuid.uuid4(),
        cwmp_url="https://acs.example.net/cwmp",
        cwmp_username="cwmp-user",
        cwmp_password="encrypted-password",
    )
    operation = SimpleNamespace(parent_id=None)
    db = MagicMock()
    db.get.return_value = ont
    session_context = MagicMock()
    session_context.__enter__.return_value = db
    session_context.__exit__.return_value = False
    result = ReconcileResult(
        success=True,
        sync_status="synced",
        actions_applied=(),
        drift_before=(),
        drift_after=(),
        observed_after=None,
        failure=None,
        duration_ms=10,
        reconciled_at=datetime.now(UTC),
    )

    with (
        patch(
            "app.tasks.ont_reconcile.db_session_adapter.session",
            return_value=session_context,
        ),
        patch(
            "app.services.network_operations.network_operations.mark_running",
            return_value=operation,
        ),
        patch(
            "app.services.network_operations.network_operations.mark_succeeded"
        ) as mark_succeeded,
        patch(
            "app.services.network.reconcile.core.reconcile_ont",
            return_value=result,
        ) as reconcile,
        patch(
            "app.services.network.acs_resolution.resolve_acs_for_ont",
            return_value=SimpleNamespace(server=acs),
        ),
        patch("app.services.tr069.sync_ont_acs_server") as sync_link,
    ):
        payload = reconcile_huawei_ont.run(ont_id, operation_id)

    reconcile.assert_called_once_with(
        db,
        ont_id,
        proposed_change={
            "acs_url": acs.cwmp_url,
            "acs_username": acs.cwmp_username,
            "acs_password_ref": acs.cwmp_password,
        },
        mode="sweep",
        timeout_sec=120,
    )
    sync_link.assert_called_once_with(db, ont, acs.id)
    mark_succeeded.assert_called_once()
    assert payload["success"] is True
