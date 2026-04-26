"""Tests for bulk direct ONT provisioning."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID


class _SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False


def test_bulk_provision_action_executes_direct_provisioning(monkeypatch):
    from app.models.network import BulkProvisioningRunStatus
    from app.services.network.bulk_provisioning import BulkProvisioningDispatchResult
    from app.tasks.ont_bulk import execute_bulk_action

    calls: list[dict[str, Any]] = []

    def fake_bulk_provision_onts(db, ont_ids, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"ont_ids": ont_ids, **kwargs})
        return BulkProvisioningDispatchResult(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            correlation_key="bulk-test",
            status=BulkProvisioningRunStatus.running,
            total=2,
            processed=2,
            skipped=0,
            orchestrator_task_id=None,
        )

    monkeypatch.setattr(
        "app.services.network.bulk_provisioning.bulk_provision_onts",
        fake_bulk_provision_onts,
    )

    result = execute_bulk_action.run(
        ["ont-a", "ont-b"],
        "provision",
        {
            "initiated_by": "admin",
            "max_parallel": 10,
            "chunk_delay_seconds": 20,
        },
    )

    assert result == {
        "processed": 2,
        "errors": 0,
        "skipped": 0,
        "bulk_run_id": "00000000-0000-0000-0000-000000000001",
        "correlation_key": "bulk-test",
        "orchestrator_task_id": None,
        "provisioning_mode": "direct",
    }
    assert calls == [
        {
            "ont_ids": ["ont-a", "ont-b"],
            "max_workers": 10,
            "chunk_delay_seconds": 20,
            "initiated_by": "admin",
            "correlation_key": None,
            "dry_run": False,
            "allow_low_optical_margin": False,
            "step_data": {},
            "metadata": {"source": "ont_bulk_action"},
        }
    ]


def test_bulk_direct_compat_task_dedupes_and_executes(monkeypatch):
    from app.tasks.ont_provisioning import queue_bulk_provisioning

    provisioned: list[str] = []

    def fake_provision(db, ont_id, **kwargs):  # type: ignore[no-untyped-def]
        provisioned.append(ont_id)
        return SimpleNamespace(
            success=True,
            message=f"provisioned {ont_id}",
            to_dict=lambda: {"success": True, "message": f"provisioned {ont_id}"},
        )

    monkeypatch.setattr(
        "app.services.network.ont_provisioning.orchestrator.provision_ont_from_desired_config",
        fake_provision,
    )

    result = queue_bulk_provisioning.run(
        ["ont-1", "ont-2", "ont-1", "", "ont-3"],
        dry_run=True,
        initiated_by="admin",
        max_parallel=2,
        chunk_delay_seconds=30,
    )

    assert result["processed"] == 3
    assert result["skipped"] == 2
    assert result["errors"] == 0
    assert result["chunks"] == 1
    assert [task["ont_id"] for task in result["tasks"]] == ["ont-1", "ont-2", "ont-3"]
    assert provisioned == ["ont-1", "ont-2", "ont-3"]


def test_bulk_direct_compat_task_uses_bulk_item_correlation(
    db_session,
    monkeypatch,
):
    import app.tasks.ont_provisioning as provisioning_task_module
    from app.models.network import (
        BulkProvisioningItem,
        BulkProvisioningItemStatus,
        BulkProvisioningRun,
        BulkProvisioningRunStatus,
        OntUnit,
    )
    from app.tasks.ont_provisioning import queue_bulk_provisioning

    ont = OntUnit(serial_number="BULK-DIRECT-CORR")
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    run = BulkProvisioningRun(
        status=BulkProvisioningRunStatus.running,
        correlation_key="bulk-corr",
        total_count=1,
    )
    db_session.add(run)
    db_session.flush()
    item = BulkProvisioningItem(
        run_id=run.id,
        requested_ont_id=str(ont.id),
        ont_unit_id=ont.id,
        status=BulkProvisioningItemStatus.pending,
        correlation_key=f"bulk-corr:ont:{ont.id}",
    )
    db_session.add(item)
    db_session.commit()

    captured: dict[str, Any] = {}

    def fake_provision(db, ont_id, **kwargs):  # type: ignore[no-untyped-def]
        captured["ont_id"] = ont_id
        return SimpleNamespace(
            success=True,
            message="ok",
            to_dict=lambda: {"success": True, "message": "ok"},
        )

    monkeypatch.setattr(
        "app.services.network.ont_provisioning.orchestrator.provision_ont_from_desired_config",
        fake_provision,
    )
    monkeypatch.setattr(
        provisioning_task_module.db_session_adapter,
        "read_session",
        lambda: _SessionContext(db_session),
    )
    monkeypatch.setattr(
        provisioning_task_module.db_session_adapter,
        "session",
        lambda: _SessionContext(db_session),
    )

    result = queue_bulk_provisioning.run(
        [str(ont.id)],
        bulk_run_id=str(run.id),
    )

    assert result["bulk_run_id"] == str(run.id)
    assert result["processed"] == 1
    assert result["tasks"][0]["bulk_item_id"] == str(item.id)
    assert result["tasks"][0]["correlation_key"] == item.correlation_key
    assert captured["ont_id"] == str(ont.id)
    db_session.refresh(item)
    assert item.status == BulkProvisioningItemStatus.succeeded
