"""Tests for Zabbix autofind fallback handling."""

from __future__ import annotations

from unittest.mock import patch

from app.api.zabbix_webhook import ZabbixAlertPayload, receive_zabbix_alert
from app.models.network import OLTDevice
from app.services.autofind_trigger import AutofindTriggerResult


def _payload(**overrides) -> ZabbixAlertPayload:
    data = {
        "triggerId": "trigger-1",
        "triggerName": "OLT ONTAUTOFIND event",
        "triggerStatus": "PROBLEM",
        "triggerSeverity": "Warning",
        "hostId": "10101",
        "hostName": "Fallback-OLT",
        "hostIp": "10.0.0.50",
        "eventId": "event-1",
        "itemName": "ONT autofind syslog",
        "itemValue": "1",
        "tags": {"dotmac_event": "autofind"},
    }
    data.update(overrides)
    return ZabbixAlertPayload.model_validate(data)


def test_zabbix_autofind_problem_triggers_single_olt_scan(db_session) -> None:
    olt = OLTDevice(
        name="Fallback-OLT",
        mgmt_ip="10.0.0.50",
        zabbix_host_id="10101",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()

    result = AutofindTriggerResult(
        triggered=True,
        olt_id=str(olt.id),
        olt_name=olt.name,
        task_id="autofind-task-1",
    )
    with patch(
        "app.api.zabbix_webhook.trigger_autofind_by_identifier",
        return_value=result,
    ) as trigger:
        response = receive_zabbix_alert(_payload(), db=db_session)

    assert response.status == "ok"
    assert response.alert_id is not None
    assert response.autofind_triggered is True
    assert response.autofind_task_id == "autofind-task-1"
    trigger.assert_called_once_with(
        db=db_session,
        identifier=str(olt.id),
        source="zabbix",
    )


def test_zabbix_non_autofind_problem_does_not_trigger_scan(db_session) -> None:
    payload = _payload(
        triggerName="OLT CPU high",
        itemName="CPU utilization",
        itemKey="system.cpu.util",
        tags={"service": "monitoring"},
    )

    with patch("app.api.zabbix_webhook.trigger_autofind_by_identifier") as trigger:
        response = receive_zabbix_alert(payload, db=db_session)

    assert response.status == "ok"
    assert response.alert_id is not None
    assert response.autofind_triggered is False
    assert response.autofind_task_id is None
    trigger.assert_not_called()


def test_zabbix_recovery_does_not_trigger_autofind(db_session) -> None:
    payload = _payload(triggerStatus="OK")

    with patch("app.api.zabbix_webhook.trigger_autofind_by_identifier") as trigger:
        response = receive_zabbix_alert(payload, db=db_session)

    assert response.status == "ok"
    assert response.autofind_triggered is False
    trigger.assert_not_called()
