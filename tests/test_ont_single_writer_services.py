"""Break-glass gate and lifecycle admission for the ONT single-writer rule."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.domain_errors import DomainError
from app.services.network import break_glass
from app.services.network.ont_service_configuration import (
    ManagementConfigurationChange,
    OntConfigurationSection,
    WifiConfigurationChange,
)
from app.services.web_network_ont_actions import lifecycle_admission


def _request(roles=("admin",), auth=True):
    state = SimpleNamespace(
        auth={"roles": list(roles), "principal_type": "user"} if auth else {},
        request_id="",
    )
    return SimpleNamespace(state=state, headers={}, client=None)


# -- break-glass ---------------------------------------------------------------


@pytest.fixture
def audit_calls(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        break_glass, "log_audit_event", lambda **kwargs: calls.append(kwargs)
    )
    return calls


def test_break_glass_refuses_non_admin_without_audit(audit_calls):
    db = MagicMock()
    decision = break_glass.authorize_break_glass(
        db,
        _request(roles=("noc",)),
        action="bind_internet_wan",
        resource_type="ont",
        resource_id="ont-1",
        reason="customer is offline, verified on site",
    )
    assert not decision.allowed
    assert audit_calls == []


@pytest.mark.parametrize("reason", [None, "", "   ", "too short"])
def test_break_glass_requires_a_real_reason(audit_calls, reason):
    decision = break_glass.authorize_break_glass(
        MagicMock(),
        _request(),
        action="tr069_config_push.x",
        resource_type="tr069_device",
        resource_id="dev-1",
        reason=reason,
    )
    assert not decision.allowed
    assert audit_calls == []


def test_break_glass_records_audit_event_before_allowing(audit_calls):
    db = MagicMock()
    decision = break_glass.authorize_break_glass(
        db,
        _request(),
        action="bind_internet_wan",
        resource_type="ont",
        resource_id="ont-1",
        reason="  customer offline,\n verified on site  ",
    )
    assert decision.allowed
    assert decision.reason == "customer offline, verified on site"
    assert len(audit_calls) == 1
    call = audit_calls[0]
    assert call["action"] == "network.break_glass.bind_internet_wan"
    assert call["entity_type"] == "ont"
    assert call["entity_id"] == "ont-1"
    assert call["metadata"]["break_glass"] is True
    assert call["metadata"]["reason"] == "customer offline, verified on site"


# -- lifecycle admission -------------------------------------------------------


@pytest.fixture
def admission(monkeypatch):
    captured: dict = {}

    def fake_configure(db, command):
        captured["command"] = command
        return SimpleNamespace(
            operation_id=uuid.uuid4(), revision=7, message="Queued for delivery."
        )

    monkeypatch.setattr(lifecycle_admission, "configure_ont_service", fake_configure)
    monkeypatch.setattr(lifecycle_admission, "has_permission", lambda *a, **k: True)
    monkeypatch.setattr(lifecycle_admission, "finish_read_transaction", lambda db: None)
    monkeypatch.setattr(
        lifecycle_admission.web_admin_service, "get_actor_id", lambda request: "u-1"
    )
    monkeypatch.setattr(lifecycle_admission, "_load_ont", lambda db, ont_id: object())
    monkeypatch.setattr(
        lifecycle_admission,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "values": {
                "wifi_enabled": False,
                "wifi_ssid": "OLD",
                "wifi_channel": "6",
                "wifi_security_mode": "WPA2",
            }
        },
    )
    return captured


def test_ssid_change_carries_other_wifi_fields_forward(admission):
    result = lifecycle_admission.submit_wifi_change(
        MagicMock(), _request(), str(uuid.uuid4()), ssid="NEW"
    )
    assert result.success and result.waiting
    command = admission["command"]
    assert command.section is OntConfigurationSection.wifi
    change = command.change
    assert isinstance(change, WifiConfigurationChange)
    assert (change.ssid, change.enabled, change.channel) == ("NEW", False, "6")
    assert change.security_mode == "WPA2"
    assert change.password is None
    assert command.permission_granted is True
    assert command.context.scope == "network:ont:write"
    assert command.context.idempotency_key.startswith("ont-config:")


def test_password_change_keeps_current_ssid(admission):
    result = lifecycle_admission.submit_wifi_change(
        MagicMock(), _request(), str(uuid.uuid4()), password="correct horse 1"
    )
    assert result.success
    change = admission["command"].change
    assert change.ssid == "OLD"
    assert change.password == "correct horse 1"


def test_empty_wifi_change_is_refused_without_calling_lifecycle(admission):
    result = lifecycle_admission.submit_wifi_change(
        MagicMock(), _request(), str(uuid.uuid4())
    )
    assert not result.success
    assert "command" not in admission


def test_management_change_uses_management_section(admission):
    result = lifecycle_admission.submit_management_change(
        MagicMock(),
        _request(),
        str(uuid.uuid4()),
        ip_mode="dhcp",
        ip_address=None,
        remote_access=True,
        wcd_index=3,
    )
    assert result.success
    command = admission["command"]
    assert command.section is OntConfigurationSection.management
    assert command.change == ManagementConfigurationChange(
        ip_mode="dhcp", ip_address=None, remote_access=True, wcd_index=3
    )


def test_lifecycle_domain_error_becomes_a_failed_result(monkeypatch, admission):
    def refuse(db, command):
        raise DomainError(code="x.refused", message="No active assignment.")

    monkeypatch.setattr(lifecycle_admission, "configure_ont_service", refuse)
    result = lifecycle_admission.submit_wifi_change(
        MagicMock(), _request(), str(uuid.uuid4()), ssid="NEW"
    )
    assert not result.success
    assert result.message == "No active assignment."
    assert result.error_code == "x.refused"


def test_invalid_ont_id_is_refused(admission):
    result = lifecycle_admission.submit_section_change(
        MagicMock(),
        _request(),
        "not-a-uuid",
        section=OntConfigurationSection.wifi,
        change=WifiConfigurationChange(True, "a", None, None),
        reason="x",
    )
    assert not result.success
    assert "command" not in admission
