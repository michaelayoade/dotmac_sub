from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.network import OLTDevice, OntUnit
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.credential_crypto import encrypt_credential
from app.services.network import ont_authorization
from app.tasks import ont_authorization as ont_authorization_tasks


class _SessionProxy:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


def test_post_authorization_follow_up_queues_acs_connectivity(
    db_session, monkeypatch
):
    """Authorized ONTs must schedule the ACS reachability/bootstrap step."""
    queued: list[dict] = []

    monkeypatch.setattr(
        ont_authorization,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *args, **kwargs: (True, "Linked ONT to PON port."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "allocate_management_ip_for_ont",
        lambda *args, **kwargs: (True, "Allocated management IP.", "10.10.10.2"),
    )

    def fake_enqueue_task(*args, **kwargs):
        queued.append({"args": args, "kwargs": kwargs})
        return type(
            "Dispatch",
            (),
            {"queued": True, "task_id": "task-acs", "error": None},
        )()

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    success, message, steps = ont_authorization.run_post_authorization_follow_up(
        db_session,
        ont_unit_id="ont-1",
        olt_id="olt-1",
        fsp="0/1/1",
        serial_number="HWTCBOOTSTRAP",
        ont_id_on_olt=7,
    )

    assert success is True
    assert message == "Authorization follow-up completed."
    assert steps[-1]["name"] == "Queue TR-069 ACS connectivity"
    assert steps[-1]["success"] is True
    assert queued == [
        {
            "args": (
                "app.tasks.ont_authorization.ensure_tr069_acs_connectivity",
            ),
            "kwargs": {
                "args": ["ont-1", "olt-1", "0/1/1", 7],
                "queue": "acs",
                "correlation_id": "tr069_acs_connect:ont-1",
                "source": "post_authorization_follow_up",
                "countdown": 5,
            },
        }
    ]


def test_post_authorization_follow_up_fails_when_acs_queue_fails(
    db_session, monkeypatch
):
    """A failed ACS bootstrap dispatch makes the follow-up operation fail."""
    monkeypatch.setattr(
        ont_authorization,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *args, **kwargs: (True, "Linked ONT to PON port."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "allocate_management_ip_for_ont",
        lambda *args, **kwargs: (True, "Allocated management IP.", "10.10.10.2"),
    )
    monkeypatch.setattr(
        "app.services.queue_adapter.enqueue_task",
        lambda *args, **kwargs: SimpleNamespace(
            queued=False,
            task_id=None,
            error="broker unavailable",
        ),
    )

    success, message, steps = ont_authorization.run_post_authorization_follow_up(
        db_session,
        ont_unit_id="ont-1",
        olt_id="olt-1",
        fsp="0/1/1",
        serial_number="HWTCBOOTSTRAP",
        ont_id_on_olt=7,
    )

    assert success is False
    assert message == "Authorization follow-up failed: ACS connectivity task was not queued."
    assert steps[-1]["name"] == "Queue TR-069 ACS connectivity"
    assert steps[-1]["success"] is False
    assert "broker unavailable" in steps[-1]["message"]


def test_post_authorization_follow_up_fails_when_management_ip_not_allocated(
    db_session, monkeypatch
):
    """ACS bootstrap is not queued if management IP allocation fails."""
    queued: list[dict] = []

    monkeypatch.setattr(
        ont_authorization,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *args, **kwargs: (True, "Linked ONT to PON port."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "allocate_management_ip_for_ont",
        lambda *args, **kwargs: (False, "Management IP pool exhausted.", None),
    )

    def fake_enqueue_task(*args, **kwargs):
        queued.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(queued=True, task_id="task-acs", error=None)

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    success, message, steps = ont_authorization.run_post_authorization_follow_up(
        db_session,
        ont_unit_id="ont-1",
        olt_id="olt-1",
        fsp="0/1/1",
        serial_number="HWTCBOOTSTRAP",
        ont_id_on_olt=7,
    )

    assert success is False
    assert message == "Management IP pool exhausted."
    assert steps[-1]["name"] == "Allocate management IP"
    assert steps[-1]["success"] is False
    assert queued == []


def test_authorization_warns_when_post_auth_follow_up_not_queued(
    db_session, monkeypatch
):
    """Foreground authorization surfaces missing post-auth ACS follow-up."""
    result = ont_authorization.AuthorizationWorkflowResult(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
    )

    monkeypatch.setattr(
        ont_authorization,
        "authorize_autofind_ont",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        ont_authorization,
        "queue_post_authorization_follow_up",
        lambda *args, **kwargs: None,
    )

    response = ont_authorization.authorize_autofind_ont_and_provision_network_audited(
        db_session,
        "olt-1",
        "0/1/1",
        "HWTCWARNFOLLOWUP",
    )

    assert response.success is True
    assert response.completed_authorization is True
    assert response.status == "warning"
    assert response.message == (
        "ONT authorized, but post-authorization ACS follow-up was not queued."
    )


def test_acs_connectivity_fails_when_acs_has_no_olt_tr069_profile(
    db_session, monkeypatch
):
    """ACS intent without an OLT TR-069 profile cannot produce ACS reachability."""
    olt = OLTDevice(name="OLT-Missing-TR069-Profile", is_active=True)
    ont = OntUnit(serial_number="HWTCNOPROFILE", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.commit()

    monkeypatch.setattr(
        ont_authorization_tasks.db_session_adapter,
        "create_session",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.task_idempotency.SessionLocal",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.network.effective_ont_config.resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(),
            "values": {
                "tr069_acs_server_id": "acs-1",
                "tr069_olt_profile_id": None,
            },
        },
    )

    with pytest.raises(RuntimeError, match="no OLT TR-069 profile ID"):
        ont_authorization_tasks.ensure_tr069_acs_connectivity.run.__wrapped__(
            str(ont.id),
            str(olt.id),
            "0/1/1",
            7,
        )


def test_acs_connectivity_does_not_auto_normalize_wan_after_inform(
    db_session, monkeypatch
):
    """The ACS bootstrap path stops after ACS reachability and inform settings."""
    olt = OLTDevice(name="OLT-ACS-Bootstrap", is_active=True)
    ont = OntUnit(serial_number="HWTCBOOTACS", olt_device=olt, is_active=True)
    acs_server = Tr069AcsServer(
        name="Post Auth ACS",
        base_url="http://genieacs.example:7557",
        connection_request_username="cr-user",
        connection_request_password=encrypt_credential("cr-pass"),
        periodic_inform_interval=300,
        is_active=True,
    )
    db_session.add_all([olt, ont, acs_server])
    db_session.flush()
    db_session.add(
        Tr069CpeDevice(
            serial_number=ont.serial_number,
            ont_unit_id=ont.id,
            acs_server_id=acs_server.id,
            genieacs_device_id="ABC-ONT-HWTCBOOTACS",
            last_inform_at=None,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        ont_authorization_tasks.db_session_adapter,
        "create_session",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.task_idempotency.SessionLocal",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.network.effective_ont_config.resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": str(acs_server.id),
                "tr069_olt_profile_id": 7,
            },
        },
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda olt: SimpleNamespace(
            configure_management_batch=lambda spec: SimpleNamespace(
                success=True,
                message="bound tr069",
                data={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda db, ont_id: SimpleNamespace(
            success=True,
            message="Device registered in ACS",
            duration_ms=10,
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_action_network.set_connection_request_credentials",
        lambda *args, **kwargs: SimpleNamespace(
            success=True,
            message="CR credentials set",
        ),
    )

    result = ont_authorization_tasks.ensure_tr069_acs_connectivity.run.__wrapped__(
        str(ont.id),
        str(olt.id),
        "0/1/1",
        7,
    )

    assert result["success"] is True
    step_names = [step["name"] for step in result["steps"]]
    assert "Run batched OLT management setup" in step_names
    assert "Wait for ACS inform" in step_names
    assert "Apply ACS inform settings" in step_names
    assert "Normalize WAN structure" not in step_names


def test_missing_acs_connection_request_credentials_does_not_retry(
    db_session, monkeypatch
):
    """Missing ACS CR credentials is static config, not a retryable bootstrap miss."""
    olt = OLTDevice(name="OLT-ACS-No-CR-Credentials", is_active=True)
    ont = OntUnit(serial_number="HWTCNOCRCRED", olt_device=olt, is_active=True)
    acs_server = Tr069AcsServer(
        name="Post Auth ACS Missing CR",
        base_url="http://genieacs.example:7557",
        connection_request_username="",
        connection_request_password=None,
        periodic_inform_interval=300,
        is_active=True,
    )
    db_session.add_all([olt, ont, acs_server])
    db_session.flush()
    db_session.add(
        Tr069CpeDevice(
            serial_number=ont.serial_number,
            ont_unit_id=ont.id,
            acs_server_id=acs_server.id,
            genieacs_device_id="ABC-ONT-HWTCNOCRCRED",
            last_inform_at=None,
        )
    )
    db_session.commit()

    queued: list[dict] = []

    monkeypatch.setattr(
        ont_authorization_tasks.db_session_adapter,
        "create_session",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.task_idempotency.SessionLocal",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.network.effective_ont_config.resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": str(acs_server.id),
                "tr069_olt_profile_id": 7,
            },
        },
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda olt: SimpleNamespace(
            configure_management_batch=lambda spec: SimpleNamespace(
                success=True,
                message="bound tr069",
                data={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda db, ont_id: SimpleNamespace(
            success=True,
            message="Device registered in ACS",
            duration_ms=10,
        ),
    )

    def fake_enqueue_task(*args, **kwargs):
        queued.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(queued=True, task_id="retry-task", error=None)

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    with pytest.raises(RuntimeError, match="connection-request credentials"):
        ont_authorization_tasks.ensure_tr069_acs_connectivity.run.__wrapped__(
            str(ont.id),
            str(olt.id),
            "0/1/1",
            7,
        )

    assert queued == []


def test_acs_connectivity_retry_uses_default_backoff(
    db_session, monkeypatch
):
    """Transient ACS bootstrap retries use a nonzero default countdown."""
    olt = OLTDevice(name="OLT-ACS-Retry-Backoff", is_active=True)
    ont = OntUnit(serial_number="HWTCRETRYWAIT", olt_device=olt, is_active=True)
    acs_server = Tr069AcsServer(
        name="Post Auth ACS Retry",
        base_url="http://genieacs.example:7557",
        connection_request_username="cr-user",
        connection_request_password=encrypt_credential("cr-pass"),
        periodic_inform_interval=300,
        is_active=True,
    )
    db_session.add_all([olt, ont, acs_server])
    db_session.flush()
    db_session.add(
        Tr069CpeDevice(
            serial_number=ont.serial_number,
            ont_unit_id=ont.id,
            acs_server_id=acs_server.id,
            genieacs_device_id="ABC-ONT-HWTCRETRYWAIT",
            last_inform_at=None,
        )
    )
    db_session.commit()

    queued: list[dict] = []

    monkeypatch.setattr(
        ont_authorization_tasks.db_session_adapter,
        "create_session",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.task_idempotency.SessionLocal",
        lambda: _SessionProxy(db_session),
    )
    monkeypatch.setattr(
        "app.services.network.effective_ont_config.resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": str(acs_server.id),
                "tr069_olt_profile_id": 7,
                "mgmt_ip_address": "10.10.10.2",
                "mgmt_vlan": 100,
            },
        },
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda olt: SimpleNamespace(
            configure_management_batch=lambda spec: SimpleNamespace(
                success=False,
                message="OLT busy",
                data={},
            )
        ),
    )

    def fake_enqueue_task(*args, **kwargs):
        queued.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(queued=True, task_id="retry-task", error=None)

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    with pytest.raises(RuntimeError, match="Batched OLT management setup failed"):
        ont_authorization_tasks.ensure_tr069_acs_connectivity.run.__wrapped__(
            str(ont.id),
            str(olt.id),
            "0/1/1",
            7,
        )

    assert queued
    assert queued[0]["kwargs"]["countdown"] == 60
    assert queued[0]["kwargs"]["kwargs"]["retry_countdown_seconds"] == 60
