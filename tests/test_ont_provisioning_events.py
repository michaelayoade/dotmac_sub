"""Tests for ONT provisioning event logging."""

from __future__ import annotations

from sqlalchemy import select


def test_record_ont_provisioning_event_persists_step_outcome(db_session) -> None:
    from app.models.network import OntProvisioningEventStatus, OntUnit
    from app.services.network.ont_provisioning.result import StepResult
    from app.services.network.provisioning_events import (
        list_ont_provisioning_events,
        record_ont_provisioning_event,
    )

    ont = OntUnit(serial_number="TEST-PROV-EVENT-001")
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    result = StepResult(
        step_name="configure_wifi",
        success=True,
        message="WiFi configured",
        duration_ms=42,
        data={"ssid": "customer-net"},
    )
    event = record_ont_provisioning_event(
        db_session,
        ont,
        "configure_wifi",
        result,
        event_data={"source": "unit-test"},
        correlation_key="order-123",
    )
    db_session.flush()

    assert event.ont_unit_id == ont.id
    assert event.action == "step_completed"
    assert event.status == OntProvisioningEventStatus.succeeded
    assert event.message == "WiFi configured"
    assert event.duration_ms == 42
    assert event.event_data == {"ssid": "customer-net", "source": "unit-test"}
    assert event.compensation_applied is False
    assert event.correlation_key == "order-123"

    events = list_ont_provisioning_events(db_session, ont.id)
    assert [item.id for item in events] == [event.id]


def test_record_step_appends_event_log_entry(db_session) -> None:
    from app.models.network import (
        OntProvisioningEvent,
        OntProvisioningEventStatus,
        OntUnit,
    )
    from app.services.network.ont_provision_steps import _record_step
    from app.services.network.ont_provisioning.result import StepResult

    ont = OntUnit(serial_number="TEST-PROV-EVENT-002")
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    result = StepResult(
        step_name="wait_tr069_bootstrap",
        success=False,
        message="Waiting for first inform",
        waiting=True,
        data={"attempt": 3},
    )
    _record_step(db_session, ont, "wait_tr069_bootstrap", result)
    db_session.flush()

    event = db_session.scalar(
        select(OntProvisioningEvent).where(
            OntProvisioningEvent.ont_unit_id == ont.id
        )
    )
    assert event is not None
    assert event.step_name == "wait_tr069_bootstrap"
    assert event.status == OntProvisioningEventStatus.waiting
    assert event.event_data == {"attempt": 3}


def test_download_firmware_records_provisioning_event(
    db_session,
    monkeypatch,
) -> None:
    from app.models.network import (
        OntProvisioningEvent,
        OntProvisioningEventStatus,
        OntUnit,
    )
    from app.services.network import ont_provision_steps
    from app.services.network.ont_action_common import ActionResult

    ont = OntUnit(serial_number="TEST-FW-DOWNLOAD")
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    class FakeWriter:
        def firmware_upgrade(self, db, ont_id, firmware_image_id):
            return ActionResult(
                success=True,
                message="Firmware queued",
                data={
                    "firmware_image_id": firmware_image_id,
                    "task": {"_id": "task-1"},
                },
            )

    monkeypatch.setattr(
        ont_provision_steps,
        "_acs_config_writer",
        lambda: FakeWriter(),
    )

    result = ont_provision_steps.download_firmware(
        db_session,
        str(ont.id),
        firmware_image_id="firmware-1",
    )
    db_session.flush()

    assert result.success is True
    assert result.step_name == "download_firmware"
    assert result.data["firmware_image_id"] == "firmware-1"

    event = db_session.scalar(
        select(OntProvisioningEvent).where(
            OntProvisioningEvent.ont_unit_id == ont.id
        )
    )
    assert event is not None
    assert event.step_name == "download_firmware"
    assert event.status == OntProvisioningEventStatus.succeeded
    assert event.event_data["firmware_image_id"] == "firmware-1"
