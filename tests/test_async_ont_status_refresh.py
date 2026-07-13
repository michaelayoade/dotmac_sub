from contextlib import contextmanager
from pathlib import Path

from app.celery_app import celery_app
from app.models.network import OntUnit
from app.models.network_operation import (
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.ont_actions import ActionResult, OntActions
from app.services.network_operations import network_operations
from app.services.task_reliability import TASK_RELIABILITY_CONTRACTS
from app.services.web_network_ont_actions import device_actions
from app.services.web_network_operations import build_operation_history
from app.tasks import ont_runtime_status
from app.web.admin import network_onts_actions


def _ont(db_session, serial: str = "ASYNC-REFRESH-ONT") -> OntUnit:
    ont = OntUnit(serial_number=serial, is_active=True)
    db_session.add(ont)
    db_session.commit()
    return ont


def test_queue_refresh_tracks_and_deduplicates_operation(db_session, monkeypatch):
    ont = _ont(db_session)
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ont_runtime_status.refresh_single_ont_status,
        "delay",
        lambda ont_id, operation_id: queued.append((ont_id, operation_id)),
    )

    first = device_actions.queue_refresh(db_session, str(ont.id))
    second = device_actions.queue_refresh(db_session, str(ont.id))

    assert first.success is True
    assert first.waiting is True
    assert first.operation_id
    assert second.operation_id == first.operation_id
    assert second.message == "ONT status refresh is already in progress."
    assert queued == [(str(ont.id), first.operation_id)]

    operation = network_operations.get(db_session, first.operation_id)
    assert operation.status == NetworkOperationStatus.pending
    assert operation.target_type == NetworkOperationTargetType.ont
    assert operation.input_payload == {"action": "status_refresh"}
    history = build_operation_history(db_session, "ont", str(ont.id))
    assert history[0]["title"] == "ONT Status Refresh"


def test_refresh_operation_status_is_read_only_and_target_scoped(db_session):
    ont = _ont(db_session)
    operation = network_operations.start(
        db_session,
        NetworkOperationType.olt_ont_sync,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"ont_status_refresh:{ont.id}",
        input_payload={"action": "status_refresh"},
    )
    network_operations.mark_succeeded(
        db_session,
        str(operation.id),
        output_payload={"message": "Status refreshed.", "result": {"status": "online"}},
    )
    db_session.commit()

    status = device_actions.refresh_operation_status(
        db_session, str(ont.id), str(operation.id)
    )

    assert status == {
        "success": True,
        "done": True,
        "waiting": False,
        "phase": "succeeded",
        "message": "Status refreshed.",
        "operation_id": str(operation.id),
        "result": {"status": "online"},
    }


def test_refresh_worker_persists_terminal_operation_result(db_session, monkeypatch):
    ont = _ont(db_session)
    operation = network_operations.start(
        db_session,
        NetworkOperationType.olt_ont_sync,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"ont_status_refresh:{ont.id}",
        input_payload={"action": "status_refresh"},
    )
    db_session.commit()

    @contextmanager
    def session():
        yield db_session

    monkeypatch.setattr(ont_runtime_status.db_session_adapter, "session", session)
    monkeypatch.setattr(
        OntActions,
        "refresh_status",
        lambda _db, _ont_id: ActionResult(
            success=True,
            message="Refresh complete.",
            data={"status": "online", "source": "olt"},
        ),
    )

    result = ont_runtime_status.refresh_single_ont_status.run(
        str(ont.id), str(operation.id)
    )

    db_session.expire_all()
    refreshed = network_operations.get(db_session, str(operation.id))
    assert result["success"] is True
    assert refreshed.status == NetworkOperationStatus.succeeded
    assert refreshed.output_payload == {
        "message": "Refresh complete.",
        "result": {"status": "online", "source": "olt"},
    }


def test_ont_refresh_controls_only_post_device_work():
    templates = [
        "templates/admin/network/onts/_quick_actions.html",
        "templates/admin/network/onts/_tab_diagnostics.html",
        "templates/admin/network/onts/_tab_overview.html",
    ]
    content = "\n".join(Path(path).read_text() for path in templates)

    assert 'hx-get="/admin/network/onts/{{ ont.id }}/refresh-status"' not in content
    assert 'hx-post="/admin/network/onts/{{ ont.id }}/refresh"' in content
    assert "pollRefresh(data.status_url, 90)" in content


def test_ont_refresh_task_is_routed_and_classified():
    task_name = "app.tasks.ont_runtime_status.refresh_single_ont_status"

    assert celery_app.conf.task_routes[task_name] == {"queue": "ingestion"}
    assert task_name in TASK_RELIABILITY_CONTRACTS


def test_queued_action_response_exposes_poll_contract():
    response = network_onts_actions._action_json_response(
        success=False,
        waiting=True,
        message="ONT status refresh queued.",
        action="Refresh ONT",
        operation_id="operation-1",
        status_url="/refresh-status?operation_id=operation-1",
    )

    assert response.status_code == 202
    assert response.body == (
        b'{"success":true,"message":"ONT status refresh queued.",'
        b'"phase":"waiting","waiting":true,"operation":{"action":"Refresh ONT",'
        b'"phase":"waiting","detail":"ONT status refresh queued."},'
        b'"operation_id":"operation-1",'
        b'"status_url":"/refresh-status?operation_id=operation-1"}'
    )
