"""Tests for Celery-backed bulk ONT provisioning audit."""

from __future__ import annotations

from types import SimpleNamespace


def test_bulk_provision_onts_records_run_items_and_queues_orchestrator(
    db_session,
    monkeypatch,
) -> None:
    from app.models.network import (
        BulkProvisioningItem,
        BulkProvisioningItemStatus,
        BulkProvisioningRun,
        BulkProvisioningRunStatus,
        OntUnit,
    )
    from app.services.network import bulk_provisioning

    ont_ok = OntUnit(serial_number="BULK-ONT-OK")
    ont_other = OntUnit(serial_number="BULK-ONT-OTHER")
    db_session.add_all([ont_ok, ont_other])
    db_session.commit()
    db_session.refresh(ont_ok)
    db_session.refresh(ont_other)

    monkeypatch.setattr(
        "app.services.queue_adapter.enqueue_task",
        lambda *args, **kwargs: SimpleNamespace(
            queued=True, task_id="bulk-task-1", error=None
        ),
    )

    missing_ont_id = "11111111-1111-1111-1111-111111111111"
    result = bulk_provisioning.bulk_provision_onts(
        db_session,
        [str(ont_ok.id), str(ont_other.id), str(ont_ok.id), missing_ont_id],
        max_workers=10,
        initiated_by="admin",
        correlation_key="bulk-test",
        step_data={"wait_for_acs": False},
    )

    assert result.status == BulkProvisioningRunStatus.running
    assert result.total == 4
    assert result.processed == 2
    assert result.skipped == 2
    assert result.orchestrator_task_id == "bulk-task-1"

    run = db_session.get(BulkProvisioningRun, result.run_id)
    assert run is not None
    assert run.max_workers == 10
    assert run.initiated_by == "admin"
    assert run.correlation_key == "bulk-test"
    assert run.profile_id is None

    items = list(
        db_session.query(BulkProvisioningItem)
        .filter(BulkProvisioningItem.run_id == run.id)
        .order_by(BulkProvisioningItem.requested_ont_id)
    )
    assert len(items) == 3
    assert (
        sum(1 for item in items if item.status == BulkProvisioningItemStatus.pending)
        == 2
    )
    assert (
        sum(1 for item in items if item.status == BulkProvisioningItemStatus.skipped)
        == 1
    )
    assert {item.correlation_key for item in items} == {
        f"bulk-test:ont:{item.requested_ont_id}" for item in items
    }


def test_bulk_item_completion_finalizes_run_and_events_are_queryable(
    db_session,
    monkeypatch,
) -> None:
    from app.models.network import (
        BulkProvisioningItem,
        BulkProvisioningItemStatus,
        BulkProvisioningRunStatus,
        OntProvisioningEvent,
        OntUnit,
    )
    from app.services.network import bulk_provisioning
    from app.services.network.ont_provisioning.result import StepResult
    from app.services.network.provisioning_events import (
        provisioning_correlation,
        record_ont_provisioning_event,
    )

    ont_ok = OntUnit(serial_number="BULK-ONT-OK-DONE")
    ont_fail = OntUnit(serial_number="BULK-ONT-FAIL-DONE")
    db_session.add_all([ont_ok, ont_fail])
    db_session.commit()
    db_session.refresh(ont_ok)
    db_session.refresh(ont_fail)

    monkeypatch.setattr(
        "app.services.queue_adapter.enqueue_task",
        lambda *args, **kwargs: SimpleNamespace(
            queued=True, task_id="bulk-task-2", error=None
        ),
    )

    result = bulk_provisioning.bulk_provision_onts(
        db_session,
        [str(ont_ok.id), str(ont_fail.id)],
        correlation_key="bulk-events",
    )
    items = list(
        db_session.query(BulkProvisioningItem)
        .filter(BulkProvisioningItem.run_id == result.run_id)
        .order_by(BulkProvisioningItem.requested_ont_id)
    )
    item_by_ont = {item.ont_unit_id: item for item in items}

    ok_item = item_by_ont[ont_ok.id]
    fail_item = item_by_ont[ont_fail.id]
    bulk_provisioning.mark_bulk_item_completed(
        db_session,
        ok_item.id,
        {"success": True, "waiting": False, "message": "ok"},
    )
    bulk_provisioning.mark_bulk_item_completed(
        db_session,
        fail_item.id,
        {"success": False, "waiting": False, "message": "boom"},
    )

    with provisioning_correlation(ok_item.correlation_key):
        record_ont_provisioning_event(
            db_session,
            ont_ok,
            "provision_reconciled",
            StepResult("provision_reconciled", True, "ok"),
        )

    with provisioning_correlation(fail_item.correlation_key):
        record_ont_provisioning_event(
            db_session,
            ont_fail,
            "provision_reconciled",
            StepResult("provision_reconciled", False, "boom"),
        )
    db_session.commit()

    run = bulk_provisioning.get_bulk_provisioning_run(db_session, result.run_id)
    assert run is not None
    assert run.status == BulkProvisioningRunStatus.partial
    assert run.succeeded_count == 1
    assert run.failed_count == 1

    db_session.refresh(ok_item)
    db_session.refresh(fail_item)
    assert ok_item.status == BulkProvisioningItemStatus.succeeded
    assert fail_item.status == BulkProvisioningItemStatus.failed

    events = bulk_provisioning.list_bulk_provisioning_events(db_session, result.run_id)
    assert len(events) == 2
    assert {event.correlation_key for event in events} == {
        ok_item.correlation_key,
        fail_item.correlation_key,
    }
    assert db_session.query(OntProvisioningEvent).count() == 2


def test_waiting_bulk_item_keeps_run_open(db_session):
    from app.models.network import (
        BulkProvisioningItem,
        BulkProvisioningItemStatus,
        BulkProvisioningRun,
        BulkProvisioningRunStatus,
        OntUnit,
    )
    from app.services.network.bulk_provisioning import mark_bulk_item_completed

    ont = OntUnit(serial_number="BULK-WAITING")
    run = BulkProvisioningRun(
        status=BulkProvisioningRunStatus.running,
        correlation_key="bulk-waiting",
        total_count=1,
    )
    db_session.add_all([ont, run])
    db_session.flush()
    item = BulkProvisioningItem(
        run_id=run.id,
        requested_ont_id=str(ont.id),
        ont_unit_id=ont.id,
        status=BulkProvisioningItemStatus.running,
        correlation_key=f"bulk-waiting:ont:{ont.id}",
    )
    db_session.add(item)
    db_session.flush()

    mark_bulk_item_completed(
        db_session,
        item.id,
        {"success": True, "waiting": True, "message": "waiting for ACS"},
    )

    assert item.status == BulkProvisioningItemStatus.waiting
    assert item.completed_at is None
    assert run.status == BulkProvisioningRunStatus.running
    assert run.completed_at is None
