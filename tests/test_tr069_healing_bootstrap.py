from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.network import (
    OLTDevice,
    OntAuthorizationStatus,
    OntUnit,
    OnuOnlineStatus,
)
from app.models.task_execution import TaskExecution, TaskExecutionStatus
from app.tasks import tr069 as tr069_tasks


class _SessionProxy:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


def _online_silent_ont(db_session) -> OntUnit:
    olt = OLTDevice(name="Heal Bootstrap OLT", mgmt_ip="198.51.100.24", is_active=True)
    ont = OntUnit(
        serial_number="HEAL-BOOTSTRAP-001",
        olt_device=olt,
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        olt_status=OnuOnlineStatus.online,
        acs_last_inform_at=None,
        board="0/1",
        port="1",
        external_id="5",
    )
    db_session.add_all([olt, ont])
    db_session.commit()
    db_session.refresh(ont)
    return ont


def _patch_healing_dependencies(monkeypatch, db_session, queued):
    monkeypatch.setattr(
        tr069_tasks.db_session_adapter,
        "create_session",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.network.effective_ont_config.resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(
                tr069_acs_server_id="acs-1",
                tr069_olt_profile_id=7,
                management_vlan=SimpleNamespace(tag=200),
            ),
            "values": {
                "tr069_acs_server_id": "acs-1",
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": 200,
            },
        },
    )

    def _enqueue_task(*args, **kwargs):
        queued.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(queued=True, task_id="queued-bootstrap", error=None)

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", _enqueue_task)


def test_healing_cools_down_recent_failed_bootstrap(db_session, monkeypatch):
    ont = _online_silent_ont(db_session)
    db_session.add(
        TaskExecution(
            task_name="app.tasks.ont_authorization.ensure_tr069_acs_connectivity",
            idempotency_key=(
                "app.tasks.ont_authorization.ensure_tr069_acs_connectivity:"
                f"tr069_connect:{ont.id}:attempt:1"
            ),
            status=TaskExecutionStatus.failed,
            created_at=datetime.now(UTC) - timedelta(seconds=30),
            completed_at=datetime.now(UTC) - timedelta(seconds=20),
        )
    )
    db_session.commit()
    queued: list[dict] = []
    _patch_healing_dependencies(monkeypatch, db_session, queued)

    result = tr069_tasks.heal_online_silent_onts.run(batch_size=10, stale_minutes=15)

    assert result["bootstrapped"] == 0
    assert result["skipped"] == 1
    assert result["cooled_down"] == 1
    assert queued == []


def test_healing_requeues_old_failed_bootstrap_after_cooldown(db_session, monkeypatch):
    ont = _online_silent_ont(db_session)
    db_session.add(
        TaskExecution(
            task_name="app.tasks.ont_authorization.ensure_tr069_acs_connectivity",
            idempotency_key=(
                "app.tasks.ont_authorization.ensure_tr069_acs_connectivity:"
                f"tr069_connect:{ont.id}:attempt:1"
            ),
            status=TaskExecutionStatus.failed,
            created_at=datetime.now(UTC) - timedelta(hours=2),
            completed_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    db_session.commit()
    queued: list[dict] = []
    _patch_healing_dependencies(monkeypatch, db_session, queued)

    result = tr069_tasks.heal_online_silent_onts.run(batch_size=10, stale_minutes=15)

    assert result["bootstrapped"] == 1
    assert result["skipped"] == 0
    assert len(queued) == 1
    assert queued[0]["kwargs"]["source"] == "heal_online_silent_onts"


def test_healing_skips_when_bootstrap_attempt_is_running(db_session, monkeypatch):
    ont = _online_silent_ont(db_session)
    db_session.add(
        TaskExecution(
            task_name="app.tasks.ont_authorization.ensure_tr069_acs_connectivity",
            idempotency_key=(
                "app.tasks.ont_authorization.ensure_tr069_acs_connectivity:"
                f"tr069_connect:{ont.id}:attempt:2"
            ),
            status=TaskExecutionStatus.running,
            created_at=datetime.now(UTC) - timedelta(seconds=30),
        )
    )
    db_session.commit()
    queued: list[dict] = []
    _patch_healing_dependencies(monkeypatch, db_session, queued)

    result = tr069_tasks.heal_online_silent_onts.run(batch_size=10, stale_minutes=15)

    assert result["bootstrapped"] == 0
    assert result["skipped"] == 1
    assert queued == []
