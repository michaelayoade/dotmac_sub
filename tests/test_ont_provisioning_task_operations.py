from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select


class _SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False


def test_provision_ont_task_marks_waiting_result_as_waiting_operation(
    db_session,
    monkeypatch,
) -> None:
    import app.tasks.ont_provisioning as provisioning_task_module
    from app.models.network import OntUnit
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationType,
    )
    from app.services.network.ont_provisioning.result import StepResult
    from app.tasks.ont_provisioning import provision_ont

    ont = OntUnit(serial_number="WAITING-PROVISION-TASK")
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    def fake_apply_authorization_baseline(db, ont_id, **kwargs):
        return StepResult(
            "authorization_baseline",
            True,
            "Authorization baseline applied; waiting for ACS bootstrap verification.",
            123,
            waiting=True,
            data={"waiting_reason": "acs_bootstrap_verify"},
        )

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.apply_authorization_baseline",
        fake_apply_authorization_baseline,
    )
    monkeypatch.setattr(
        "app.services.queue_adapter.enqueue_task",
        lambda *args, **kwargs: SimpleNamespace(
            queued=True, task_id="bootstrap-task", error=None
        ),
    )
    monkeypatch.setattr(
        provisioning_task_module.db_session_adapter,
        "session",
        lambda: _SessionContext(db_session),
    )

    result = provision_ont.run(str(ont.id), initiated_by="admin")

    assert result["success"] is True
    assert result["waiting"] is True
    assert result["operation_id"]

    op = db_session.scalars(
        select(NetworkOperation).where(
            NetworkOperation.operation_type == NetworkOperationType.ont_provision,
            NetworkOperation.target_id == ont.id,
        )
    ).one()
    assert op.status == NetworkOperationStatus.waiting
    assert op.waiting_reason == "acs_bootstrap_verify"
    assert op.output_payload["waiting"] is True
    assert op.output_payload["duration_ms"] == 123
    assert op.output_payload["follow_up_queued"] is True

    child = db_session.scalars(
        select(NetworkOperation).where(NetworkOperation.parent_id == op.id)
    ).one()
    assert child.status == NetworkOperationStatus.waiting


def test_bootstrap_confirmation_completes_parent_and_bulk_item(db_session):
    from app.models.network import (
        BulkProvisioningItem,
        BulkProvisioningItemStatus,
        BulkProvisioningRun,
        BulkProvisioningRunStatus,
        OntUnit,
    )
    from app.models.network_operation import (
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations
    from app.tasks.tr069 import _sync_bootstrap_parent

    ont = OntUnit(serial_number="BOOTSTRAP-CONFIRMED")
    run = BulkProvisioningRun(
        status=BulkProvisioningRunStatus.running,
        correlation_key="bulk-confirmed",
        total_count=1,
    )
    db_session.add_all([ont, run])
    db_session.flush()
    item = BulkProvisioningItem(
        run_id=run.id,
        requested_ont_id=str(ont.id),
        ont_unit_id=ont.id,
        status=BulkProvisioningItemStatus.waiting,
        correlation_key=f"bulk-confirmed:ont:{ont.id}",
    )
    db_session.add(item)
    db_session.flush()

    parent = network_operations.start(
        db_session,
        NetworkOperationType.ont_provision,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"provision:{ont.id}",
        input_payload={"bulk_item_id": str(item.id)},
    )
    network_operations.mark_running(db_session, str(parent.id))
    child = network_operations.start(
        db_session,
        NetworkOperationType.tr069_bootstrap,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"tr069_bootstrap:{ont.id}",
        parent_id=str(parent.id),
    )
    network_operations.mark_running(db_session, str(child.id))
    network_operations.mark_succeeded(
        db_session,
        str(child.id),
        output_payload={"success": True},
    )

    _sync_bootstrap_parent(
        db_session,
        operation_id=str(child.id),
        ont_id=str(ont.id),
        payload={"success": True, "waiting": False, "message": "confirmed"},
    )

    assert parent.status == NetworkOperationStatus.succeeded
    assert parent.output_payload["device_confirmation"]["success"] is True
    assert item.status == BulkProvisioningItemStatus.succeeded
    assert run.status == BulkProvisioningRunStatus.succeeded


def test_inform_apply_completes_waiting_bootstrap_parent(db_session):
    from app.models.network import OntUnit
    from app.models.network_operation import (
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network.ont_provisioning.result import StepResult
    from app.services.network_operations import network_operations
    from app.tasks.tr069 import _complete_waiting_bootstrap_after_inform

    ont = OntUnit(serial_number="BOOTSTRAP-INFORM-CONFIRMED")
    db_session.add(ont)
    db_session.flush()
    parent = network_operations.start(
        db_session,
        NetworkOperationType.ont_provision,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"provision:{ont.id}",
    )
    network_operations.mark_running(db_session, str(parent.id))
    child = network_operations.start(
        db_session,
        NetworkOperationType.tr069_bootstrap,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"tr069_bootstrap:{ont.id}",
        parent_id=str(parent.id),
    )
    network_operations.mark_running(db_session, str(child.id))
    network_operations.mark_waiting(db_session, str(child.id), "next_inform")
    network_operations.update_parent_status(db_session, str(parent.id))

    completed = _complete_waiting_bootstrap_after_inform(
        db_session,
        ont_id=str(ont.id),
        result=StepResult(
            "apply_saved_service_config",
            True,
            "Saved ONT service config applied.",
            duration_ms=123,
        ),
        reason="stale_inform_reconnect",
    )

    assert completed is True
    assert child.status == NetworkOperationStatus.succeeded
    assert parent.status == NetworkOperationStatus.succeeded
    assert parent.output_payload["device_confirmation"]["confirmation_source"] == (
        "stale_inform_reconnect"
    )


def test_admin_provision_route_queues_device_write(db_session, monkeypatch):
    from app.web.admin import network_onts_provisioning

    monkeypatch.setattr(
        network_onts_provisioning,
        "can_manage_ont_from_request",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "actor_label",
        lambda request: "admin",
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "log_network_action_result",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.queue_adapter.enqueue_task",
        lambda *args, **kwargs: SimpleNamespace(
            queued=True, task_id="provision-task", error=None
        ),
    )

    response = network_onts_provisioning.provision_ont_direct(
        SimpleNamespace(headers={}),
        "00000000-0000-0000-0000-000000000001",
        dry_run=False,
        async_execution=False,
        db=db_session,
    )

    payload = json.loads(response.body)
    assert response.status_code == 202
    assert payload["waiting"] is True
    assert payload["data"]["task_id"] == "provision-task"
