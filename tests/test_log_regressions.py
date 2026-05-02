import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from app.services.network import ont_metrics
from app.services.network.olt_ssh import _run_huawei_cmd
from app.web.admin.network_olts_inventory import olt_authorize_ont


class _DummyChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, value: str) -> None:
        self.sent.append(value)


def test_promql_label_selector_does_not_overescape_hyphen() -> None:
    selector = ont_metrics._build_label_selector(
        ont_serial="485754437D4510C3",  # noqa: SLF001
        ont_id="d3158608-b57d-405b-8ace-e71ba195ed33",
    )
    assert 'ont_id="d3158608-b57d-405b-8ace-e71ba195ed33"' in selector
    assert 'ont_serial=~"' in selector
    assert r"485754437D4510C3" in selector
    assert r"485754437D4510C3\-" not in selector


def test_huawei_command_defaults_accept_user_and_exec_prompts(monkeypatch) -> None:
    channel = _DummyChannel()

    def _fake_read_until_prompt(
        _channel, prompt_regex: str, timeout_sec: float = 8.0
    ) -> str:
        assert prompt_regex.startswith(r"#\s*$")
        assert timeout_sec == 12
        return "OLT>"

    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt", _fake_read_until_prompt
    )

    output = _run_huawei_cmd(channel, "display version")

    assert output == "OLT>"
    assert channel.sent == ["display version\n"]


def test_huawei_command_accepts_brace_optional_prompt(monkeypatch) -> None:
    channel = _DummyChannel()
    responses = iter(["{ |priority<K> }:", "OLT(config-if-gpon-0/1)#"])

    def _fake_read_until_prompt(
        _channel, prompt_regex: str, timeout_sec: float = 8.0
    ) -> str:
        assert prompt_regex.startswith(r"#\s*$")
        assert timeout_sec == 12
        return next(responses)

    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt", _fake_read_until_prompt
    )

    output = _run_huawei_cmd(channel, "ont ipconfig 13 8 ip-index 0 dhcp vlan 201")

    assert output == "OLT(config-if-gpon-0/1)#"
    assert channel.sent == [
        "ont ipconfig 13 8 ip-index 0 dhcp vlan 201\n",
        "\n",
    ]


def test_inventory_authorize_route_accepts_force_reauthorize_flag() -> None:
    signature = inspect.signature(olt_authorize_ont)

    assert "force_reauthorize" in signature.parameters


def test_olt_detail_template_has_no_manual_autofind_scan_action() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    assert "/autofind" not in template
    assert "Autofind ONTs" not in template


def test_force_authorize_route_runs_synchronously(
    monkeypatch,
) -> None:
    from app.web.admin import network_olts_inventory

    captured: dict[str, object] = {}

    def _fake_authorize(
        _db,
        *,
        olt_id: str,
        fsp: str,
        serial_number: str,
        force_reauthorize: bool = False,
        preset_id: str | None = None,
        request=None,
    ):
        captured.update(
            {
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
                "force_reauthorize": force_reauthorize,
                "preset_id": preset_id,
                "request": request,
            }
        )
        return True, "ONT authorized and provisioned.", "ont-123"

    monkeypatch.setattr(
        network_olts_inventory.olt_operations_service,
        "authorize_ont",
        _fake_authorize,
    )
    monkeypatch.setattr(
        network_olts_inventory.web_admin_service,
        "get_current_user",
        lambda _request: {"name": "Alice Admin"},
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/network/olts/olt-123/authorize-ont",
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["network:write"],
    }

    response = network_olts_inventory.olt_authorize_ont(
        request,
        "olt-123",
        fsp="0/1/6",
        serial_number="4857544328201B9A",
        return_to="/admin/network/onts?view=unconfigured",
        force_reauthorize="true",
        preset_id="",
        db=SimpleNamespace(rollback=lambda: None),
    )

    assert response.status_code == 303
    assert "ONT+authorized" in response.headers["location"]
    assert captured["force_reauthorize"] is True
    assert captured["fsp"] == "0/1/6"
    assert captured["serial_number"] == "4857544328201B9A"
    assert captured["request"] is request


def test_normal_authorize_route_runs_synchronously(
    monkeypatch,
) -> None:
    from app.web.admin import network_olts_inventory

    captured: dict[str, object] = {}

    def _fake_authorize(
        _db,
        *,
        olt_id: str,
        fsp: str,
        serial_number: str,
        force_reauthorize: bool = False,
        preset_id: str | None = None,
        request=None,
    ):
        captured.update(
            {
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
                "force_reauthorize": force_reauthorize,
                "preset_id": preset_id,
                "request": request,
            }
        )
        return True, "ONT authorized.", "ont-456"

    monkeypatch.setattr(
        network_olts_inventory.olt_operations_service,
        "authorize_ont",
        _fake_authorize,
    )
    monkeypatch.setattr(
        network_olts_inventory.web_admin_service,
        "get_current_user",
        lambda _request: {"name": "Alice Admin"},
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/network/olts/olt-123/authorize-ont",
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["network:write"],
    }

    response = network_olts_inventory.olt_authorize_ont(
        request,
        "olt-123",
        fsp="0/1/6",
        serial_number="4857544328201B9A",
        return_to="/admin/network/onts?view=unconfigured",
        force_reauthorize="",
        preset_id="",
        db=SimpleNamespace(rollback=lambda: None),
    )

    assert response.status_code == 303
    assert "ONT+authorized" in response.headers["location"]
    assert captured["force_reauthorize"] is False
    assert captured["fsp"] == "0/1/6"
    assert captured["serial_number"] == "4857544328201B9A"
    assert captured["request"] is request


def test_olt_detail_template_defaults_missing_acs_prefill() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    assert "acs_prefill|default({})" in template
    assert "acs_prefill.cwmp_url" not in template


def test_olt_detail_template_uses_operator_focused_tabs() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    for tab in (
        "overview",
        "network-resources",
        "inventory",
        "provisioning",
        "operations",
    ):
        assert f"activeTab === '{tab}'" in template
        assert template.count(f"x-show=\"activeTab === '{tab}'\"") == 1

    for legacy_tab in (
        "ports",
        "onts",
        "autofind",
        "tr069",
        "config",
        "settings",
        "subscribers",
        "terminal",
        "activity",
        "events",
        "profiles",
    ):
        assert f"activeTab === '{legacy_tab}'" not in template

    assert "normalizeTab(tab)" in template
    assert "Subscriber Impact" not in template
    assert "Runtime and hardware state" not in template
    nav_section = template[
        template.index("{# Tab Navigation #}") : template.index(
            "{# ===================== OVERVIEW TAB"
        )
    ]
    assert "Profiles" not in nav_section
