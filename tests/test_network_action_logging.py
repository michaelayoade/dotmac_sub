import logging

from app.services.network.action_logging import (
    log_network_action_result,
    looks_like_prerequisite_failure,
)


def test_prerequisite_failure_classifier_covers_olt_and_ont_messages():
    assert looks_like_prerequisite_failure("No TR-069 device linked")
    assert looks_like_prerequisite_failure("OLT not found")
    assert looks_like_prerequisite_failure("Missing ONT selection or target profile")
    assert looks_like_prerequisite_failure("SNMP test failed: no response from device")
    assert not looks_like_prerequisite_failure("Firmware upgrade command rejected")


def test_log_network_action_result_emits_structured_error(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.services.network.action_logging.web_admin_service.get_current_user",
        lambda _request: {"email": "operator@example.test"},
    )

    caplog.set_level(logging.ERROR, logger="app.services.network.action_logging")
    log_network_action_result(
        request=object(),
        resource_type="olt",
        resource_id="olt-123",
        action="Test SSH Connection",
        success=False,
        message="OLT not found",
        metadata={"source": "unit-test"},
    )

    record = next(
        item
        for item in caplog.records
        if item.getMessage().startswith(
            "Network action blocked by missing prerequisite"
        )
    )
    assert record.event == "network_action_prerequisite_blocked"
    assert record.network_resource_type == "olt"
    assert record.network_resource_id == "olt-123"
    assert record.network_action == "Test SSH Connection"
    assert record.actor == "operator@example.test"
    assert record.reason == "OLT not found"
    assert record.metadata == {"source": "unit-test"}


def test_log_network_action_result_ignores_successes(caplog):
    caplog.set_level(logging.ERROR, logger="app.services.network.action_logging")
    log_network_action_result(
        request=None,
        resource_type="ont",
        resource_id="ont-123",
        action="Refresh ONT",
        success=True,
        message="OK",
    )

    assert not caplog.records
