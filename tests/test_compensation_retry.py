from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.compensation_failure import CompensationFailure, CompensationStatus
from app.models.network import OLTDevice, OntUnit
from app.services.network import compensation_retry


def _create_failure(db_session, *, failure_count: int, last_attempted_at: datetime):
    token = uuid4().hex[:8]
    olt = OLTDevice(
        name=f"OLT-{failure_count}-{token}",
        mgmt_ip=f"olt-{failure_count}-{token}",
    )
    db_session.add(olt)
    db_session.flush()

    ont = OntUnit(
        serial_number=f"ONT-COMP-{failure_count}",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add(ont)
    db_session.flush()

    failure = CompensationFailure(
        ont_unit_id=ont.id,
        olt_device_id=olt.id,
        operation_type="provisioning",
        step_name="rollback_step",
        undo_commands=["undo test"],
        description="retry me",
        error_message="initial failure",
        failure_count=failure_count,
        last_attempted_at=last_attempted_at,
        status=CompensationStatus.pending,
    )
    db_session.add(failure)
    db_session.commit()
    return failure


def test_retry_backoff_seconds_is_exponential_and_capped():
    assert compensation_retry.retry_backoff_seconds(1) == 300
    assert compensation_retry.retry_backoff_seconds(2) == 600
    assert compensation_retry.retry_backoff_seconds(3) == 1200
    assert compensation_retry.retry_backoff_seconds(10) == 21600


def test_list_retry_due_compensations_filters_by_backoff_window(db_session):
    now = datetime.now(UTC)
    due = _create_failure(
        db_session,
        failure_count=2,
        last_attempted_at=now - timedelta(minutes=11),
    )
    _create_failure(
        db_session,
        failure_count=2,
        last_attempted_at=now - timedelta(minutes=5),
    )

    rows = compensation_retry.list_retry_due_compensations(db_session, now=now)

    assert [row.id for row in rows] == [due.id]


def test_retry_due_compensations_retries_only_due_rows(db_session, monkeypatch):
    now = datetime.now(UTC)
    due = _create_failure(
        db_session,
        failure_count=1,
        last_attempted_at=now - timedelta(minutes=10),
    )
    not_due = _create_failure(
        db_session,
        failure_count=3,
        last_attempted_at=now - timedelta(minutes=5),
    )

    calls: list[str] = []

    def _fake_retry(db, failure_id, *, resolved_by=None):
        calls.append(str(failure_id))
        failure = db.get(CompensationFailure, failure_id)
        assert failure is not None
        failure.status = CompensationStatus.resolved
        return True, "resolved"

    monkeypatch.setattr(compensation_retry, "retry_compensation", _fake_retry)

    result = compensation_retry.retry_due_compensations(db_session, now=now)

    assert result["due_count"] == 1
    assert result["retried"] == 1
    assert result["resolved"] == 1
    assert calls == [str(due.id)]
    assert (
        db_session.get(CompensationFailure, due.id).status
        == CompensationStatus.resolved
    )
    assert (
        db_session.get(CompensationFailure, not_due.id).status
        == CompensationStatus.pending
    )


def test_retry_compensation_uses_targeted_service_layer_handler(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    failure = _create_failure(
        db_session,
        failure_count=1,
        last_attempted_at=now - timedelta(minutes=10),
    )
    failure.step_name = "rollback_service_ports"
    failure.undo_commands = ["service_port_index:101", "service_port_index:102"]
    db_session.commit()

    def _fake_rollback_service_port_indices(db, ont_id, *, port_indices, expected_olt_id):
        assert ont_id == str(failure.ont_unit_id)
        assert port_indices == [101, 102]
        assert expected_olt_id == str(failure.olt_device_id)
        return type(
            "RollbackResult",
            (),
            {"success": True, "message": "Removed 2 targeted service-port(s)"},
        )()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.rollback_service_port_indices",
        _fake_rollback_service_port_indices,
    )

    success, message = compensation_retry.retry_compensation(
        db_session,
        failure.id,
        resolved_by="system:watchdog",
    )

    refreshed = db_session.get(CompensationFailure, failure.id)
    assert success is True
    assert message == "Removed 2 targeted service-port(s)"
    assert refreshed is not None
    assert refreshed.status == CompensationStatus.resolved
    assert refreshed.resolved_by == "system:watchdog"
