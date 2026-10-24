"""Route-level tests for the ONT/OLT/ACS single-writer rule (2026-10-19)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.services.network.ont_action_common import ActionResult


def _request(roles=("admin",), headers=None):
    return SimpleNamespace(
        state=SimpleNamespace(
            auth={"roles": list(roles), "principal_type": "user"}, request_id=""
        ),
        headers=headers or {},
        client=None,
        query_params={},
    )


def _boom(*args, **kwargs):
    raise AssertionError("a retired route must not reach a device writer")


def _assert_retired(response):
    assert response.status_code == 410
    payload = json.loads(response.body)
    assert payload["retired"] is True
    assert payload["success"] is False
    assert "retired" in payload["message"].lower()
    assert "HX-Trigger" in response.headers


def test_service_port_routes_are_retired_and_never_write(monkeypatch):
    from app.web.admin import network_onts

    monkeypatch.setattr(
        network_onts.web_network_service_ports_service, "handle_create", _boom
    )
    monkeypatch.setattr(
        network_onts.web_network_service_ports_service, "handle_delete", _boom
    )
    monkeypatch.setattr(
        network_onts.web_network_service_ports_service, "handle_clone", _boom
    )
    for response in (
        network_onts.ont_service_port_create("ont-1"),
        network_onts.ont_service_port_delete("ont-1", 5),
        network_onts.ont_service_port_clone("ont-1"),
    ):
        _assert_retired(response)
        assert "Configure" in json.loads(response.body)["message"]


def test_legacy_provision_route_is_retired(monkeypatch):
    from app.services.network import ont_provisioning_commands
    from app.web.admin import network_onts_provisioning

    monkeypatch.setattr(
        ont_provisioning_commands, "request_ont_provisioning", _boom, raising=True
    )
    _assert_retired(network_onts_provisioning.provision_ont_direct("ont-1"))


def test_configure_mgmt_ip_action_is_retired(monkeypatch):
    from app.web.admin import network_onts_actions

    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "configure_management_ip",
        _boom,
    )
    response = network_onts_actions.ont_configure_mgmt_ip("ont-1")
    _assert_retired(response)
    assert "Management section" in json.loads(response.body)["message"]


@pytest.mark.parametrize(
    "call",
    [
        lambda m: m.update_ont_management_ip(
            "ont-1", SimpleNamespace(mgmt_ip_mode="dhcp"), MagicMock()
        ),
        lambda m: m.update_ont_service_port("ont-1", SimpleNamespace(), MagicMock()),
        lambda m: m.move_ont("ont-1", SimpleNamespace(), MagicMock()),
    ],
)
def test_api_direct_writers_return_410(call, monkeypatch):
    from app.api import network_ont_ops
    from app.services.network import ont_write as ont_write_module

    for name in ("update_management_ip", "update_service_port", "move_ont"):
        monkeypatch.setattr(ont_write_module.ont_write, name, _boom)
    with pytest.raises(HTTPException) as excinfo:
        call(network_ont_ops)
    assert excinfo.value.status_code == 410
    assert "lifecycle" in excinfo.value.detail


def test_wifi_routes_submit_through_the_lifecycle(monkeypatch):
    from app.web.admin import network_onts_actions as mod

    seen: list[dict] = []

    def fake_submit(db, request, ont_id, **kwargs):
        seen.append(kwargs)
        return ActionResult(success=True, message="Queued.", waiting=True)

    monkeypatch.setattr(mod, "submit_wifi_change", fake_submit)
    monkeypatch.setattr(mod, "_ensure_ont_write_scope", lambda *a, **k: None)
    monkeypatch.setattr(
        mod.web_network_ont_actions_service, "set_wifi_ssid", _boom, raising=False
    )
    monkeypatch.setattr(
        mod.web_network_ont_actions_service, "set_wifi_password", _boom, raising=False
    )
    monkeypatch.setattr(
        mod.web_network_ont_actions_service,
        "force_push_wifi_password",
        _boom,
        raising=False,
    )
    db = MagicMock()
    mod.ont_set_wifi_ssid(_request(), "ont-1", db=db, ssid="NEW", timeout_sec=None)
    mod.ont_set_wifi_password(
        _request(), "ont-1", db=db, password="pw-12345678", timeout_sec=None
    )
    mod.ont_force_push_wifi_password(
        _request(), "ont-1", db=db, password="pw-12345678", timeout_sec=None
    )
    assert seen == [
        {"ssid": "NEW"},
        {"password": "pw-12345678"},
        {"password": "pw-12345678"},
    ]


def _form_request(form):
    return _request()


def test_bind_internet_wan_needs_a_reason_and_audits_first(monkeypatch):
    from app.services.network import break_glass
    from app.web.admin import network_onts_actions as mod

    order: list[str] = []
    monkeypatch.setattr(mod, "_ensure_ont_write_scope", lambda *a, **k: None)
    monkeypatch.setattr(
        mod,
        "parse_form_data_sync",
        lambda request: {"break_glass_reason": request.reason, "ssid1": "true"},
    )
    monkeypatch.setattr(
        break_glass,
        "log_audit_event",
        lambda **kwargs: order.append("audit"),
    )
    monkeypatch.setattr(
        mod.web_network_ont_actions_service,
        "bind_internet_wan",
        lambda *a, **k: order.append("bind") or ActionResult(True, "bound"),
    )

    refused = _request()
    refused.reason = "short"
    response = mod.ont_bind_internet_wan(refused, "ont-1", MagicMock())
    assert response.status_code == 400
    assert order == []

    allowed = _request()
    allowed.reason = "customer offline after OLT swap"
    response = mod.ont_bind_internet_wan(allowed, "ont-1", MagicMock())
    assert response.status_code == 200
    assert order == ["audit", "bind"]


def test_bind_internet_wan_route_requires_admin_role():
    from app.web.admin import network_onts_actions as mod

    route = next(
        r
        for r in mod.router.routes
        if r.path.endswith("/onts/{ont_id}/bind-internet-wan")
    )
    names = {getattr(d.dependency, "__name__", "") for d in route.dependencies}
    assert "_require_role" in names


def test_tr069_device_job_and_config_push_are_break_glass(monkeypatch):
    from app.services.network import break_glass
    from app.web.admin import network_tr069 as mod

    calls: list[str] = []
    monkeypatch.setattr(
        break_glass, "log_audit_event", lambda **kwargs: calls.append("audit")
    )
    monkeypatch.setattr(
        mod.web_network_tr069_service,
        "queue_device_job",
        lambda *a, **k: (
            calls.append("job")
            or SimpleNamespace(command=SimpleNamespace(value="reboot"))
        ),
    )
    monkeypatch.setattr(
        mod.web_network_tr069_service,
        "create_config_push_job",
        lambda *a, **k: calls.append("config") or SimpleNamespace(name="cfg"),
    )

    def with_form(form):
        monkeypatch.setattr(mod, "parse_form_data_sync", lambda request: form)

    db = MagicMock()
    with_form({"action": "reboot", "break_glass_reason": ""})
    response = mod.tr069_device_action("dev-1", _request(), db)
    assert (
        response.status_code == 303 and "status=error" in response.headers["location"]
    )
    with_form({"config_action": "x", "config_value": "1", "break_glass_reason": ""})
    response = mod.tr069_config_push("dev-1", _request(), db)
    assert "status=error" in response.headers["location"]
    assert calls == []

    with_form({"action": "reboot", "break_glass_reason": "stuck CPE, verified"})
    mod.tr069_device_action("dev-1", _request(), db)
    with_form(
        {
            "config_action": "x",
            "config_value": "1",
            "break_glass_reason": "stuck CPE, verified",
        }
    )
    mod.tr069_config_push("dev-1", _request(), db)
    assert calls == ["audit", "job", "audit", "config"]


def test_tr069_break_glass_routes_require_admin_role():
    from app.web.admin import network_tr069 as mod

    for suffix in (
        "/tr069/devices/{device_id}/action",
        "/tr069/devices/{device_id}/config",
    ):
        route = next(r for r in mod.router.routes if r.path.endswith(suffix))
        names = {getattr(d.dependency, "__name__", "") for d in route.dependencies}
        assert "_require_role" in names, suffix


def test_mgmt_ip_modal_submit_goes_through_lifecycle(monkeypatch):
    from starlette.datastructures import FormData

    from app.services.network import ont_web_forms
    from app.services.web_network_ont_actions import lifecycle_admission

    ont = SimpleNamespace(id="ont-1", desired_config={})
    monkeypatch.setattr(
        ont_web_forms.network_service.ont_units,
        "get_including_inactive",
        lambda db, entity_id: ont,
    )
    seen: dict = {}

    def fake_submit(db, request, ont_id, **kwargs):
        seen.update(kwargs)
        return ActionResult(success=True, message="Queued.", waiting=True, data={})

    monkeypatch.setattr(lifecycle_admission, "submit_management_change", fake_submit)
    form = FormData(
        [
            ("mgmt_ip_mode", "static_ip"),
            ("mgmt_ip_address", "192.0.2.10"),
            ("mgmt_remote_access", "true"),
        ]
    )
    result = ont_web_forms.update_mgmt_ip_from_form(
        MagicMock(), "ont-1", form, request=_request()
    )
    assert result.error is None
    assert seen == {
        "ip_mode": "static_ip",
        "ip_address": "192.0.2.10",
        "remote_access": True,
        "wcd_index": None,
    }
    assert "management.ip_mode" not in ont.desired_config
