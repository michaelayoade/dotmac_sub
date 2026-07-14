"""Huawei ONT lifecycle apply/readback and operation-ledger tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationType,
)
from app.services.network.olt_protocol_adapters import OltProtocolAdapter
from app.services.network.ont_decommission import (
    DecommissionResult,
    decommission_ont,
    decommission_ont_audited,
)


def test_deauthorize_succeeds_only_after_absence_readback(monkeypatch):
    adapter = OltProtocolAdapter(SimpleNamespace(name="Test OLT"))
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda olt, fsp, ont_id: (True, "Delete accepted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.get_ont_status",
        lambda olt, fsp, ont_id: (
            False,
            "OLT error: Failure: The ONT does not exist",
            None,
        ),
    )

    result = adapter.deauthorize_ont("0/1/2", 5)

    assert result.success is True
    assert result.data == {"verified_absent": True}


def test_deauthorize_fails_when_ont_remains_on_readback(monkeypatch):
    adapter = OltProtocolAdapter(SimpleNamespace(name="Test OLT"))
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda olt, fsp, ont_id: (True, "Delete accepted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.get_ont_status",
        lambda olt, fsp, ont_id: (
            True,
            "ONT status retrieved",
            SimpleNamespace(serial_number="HWTC1234"),
        ),
    )

    result = adapter.deauthorize_ont("0/1/2", 5)

    assert result.success is False
    assert "still exists" in result.message
    assert result.data == {"verified_absent": False}


def test_service_port_delete_succeeds_only_after_absence_readback(monkeypatch):
    adapter = OltProtocolAdapter(SimpleNamespace(name="Test OLT"))
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.delete_service_port",
        lambda olt, index: (True, "Delete accepted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_port_by_index",
        lambda olt, index: (True, "No service-port", None),
    )

    result = adapter.delete_service_port(100)

    assert result.success is True
    assert result.data == {"verified_absent": True}


def test_service_port_delete_fails_when_readback_still_finds_it(monkeypatch):
    adapter = OltProtocolAdapter(SimpleNamespace(name="Test OLT"))
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.delete_service_port",
        lambda olt, index: (True, "Delete accepted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_port_by_index",
        lambda olt, index: (True, "Found", SimpleNamespace(index=index)),
    )

    result = adapter.delete_service_port(100)

    assert result.success is False
    assert "still exists" in result.message


def _assigned_ont(db_session):
    olt = OLTDevice(name="Lifecycle OLT", mgmt_ip="10.0.0.50", is_active=True)
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTC-LIFECYCLE-1",
        is_active=True,
        olt_device_id=olt.id,
        board="0/1",
        port="2",
        external_id="5",
        desired_config={"wan": {"mode": "pppoe"}},
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.commit()
    return ont, assignment


def test_decommission_does_not_change_sot_when_olt_cleanup_is_unverified(
    db_session, monkeypatch
):
    ont, assignment = _assigned_ont(db_session)
    monkeypatch.setattr(
        "app.services.network.ont_inventory.cleanup_olt_state_for_return",
        lambda db, ont_id: (False, [], ["ONT still exists on OLT"]),
    )

    result = decommission_ont(
        db_session,
        str(ont.id),
        confirm=True,
    )

    assert result.success is False
    db_session.refresh(ont)
    db_session.refresh(assignment)
    assert ont.is_active is True
    assert ont.external_id == "5"
    assert assignment.active is True


def test_audited_decommission_records_terminal_operation(db_session, monkeypatch):
    ont = OntUnit(
        serial_number="HWTC-LIFECYCLE-2",
        is_active=True,
        desired_config={},
    )
    db_session.add(ont)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_decommission.decommission_ont",
        lambda *args, **kwargs: DecommissionResult(
            success=True,
            message="Decommissioned",
            ont_id=str(ont.id),
            serial_number=ont.serial_number,
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_web_audit.log_olt_audit_event",
        lambda *args, **kwargs: None,
    )

    result = decommission_ont_audited(db_session, str(ont.id), confirm=True)

    assert result.success is True
    operation = db_session.query(NetworkOperation).one()
    assert operation.operation_type == NetworkOperationType.ont_decommission
    assert operation.status == NetworkOperationStatus.succeeded
    assert operation.output_payload["message"] == "Decommissioned"


def test_decommission_preserves_last_device_readback(db_session, monkeypatch):
    ont = OntUnit(
        serial_number="HWTC-LIFECYCLE-3",
        is_active=True,
        desired_config={"wan": {"mode": "pppoe"}},
        tr069_last_snapshot={"software_version": "V1R2"},
        olt_observed_snapshot={"run_state": "online"},
    )
    db_session.add(ont)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_inventory.cleanup_acs_state_for_return",
        lambda db, target: (True, [], []),
    )

    result = decommission_ont(db_session, str(ont.id), confirm=True)

    assert result.success is True
    assert ont.is_active is False
    assert ont.desired_config == {}
    assert ont.tr069_last_snapshot == {"software_version": "V1R2"}
    assert ont.olt_observed_snapshot == {"run_state": "online"}
