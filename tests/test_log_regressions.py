import inspect
from pathlib import Path
from types import SimpleNamespace

from app.services import monitoring_metrics as monitoring_metrics_service
from app.services import snmp_discovery as snmp_discovery_service
from app.services.network import ont_metrics
from app.services.network.olt_ssh import _run_huawei_cmd
from app.web.admin.network_olts_inventory import olt_authorize_ont
from app.web.admin.network_olts_inventory import router as olts_inventory_router


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


def test_olt_autofind_get_route_exists_for_auth_redirect_recovery() -> None:
    route = next(
        r
        for r in olts_inventory_router.routes
        if getattr(r, "path", None) == "/network/olts/{olt_id}/autofind"
        and "GET" in getattr(r, "methods", set())
    )
    assert "GET" in route.methods


def test_inventory_authorize_route_accepts_force_reauthorize_flag() -> None:
    signature = inspect.signature(olt_authorize_ont)

    assert "force_reauthorize" in signature.parameters


def test_olt_detail_template_defaults_missing_acs_prefill() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    assert "acs_prefill|default({})" in template
    assert "acs_prefill.cwmp_url" not in template


def test_snmp_v1_bulk_walk_uses_plain_walk(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run_snmp_command(args: list[str], timeout: int) -> list[str]:
        calls.append(args)
        return []

    monkeypatch.setattr(
        snmp_discovery_service, "_run_snmp_command", _fake_run_snmp_command
    )
    device = SimpleNamespace(
        mgmt_ip="192.0.2.10",
        hostname=None,
        snmp_version="v1",
        snmp_community=None,
        snmp_port=None,
    )

    snmp_discovery_service._run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.2")

    assert calls
    assert calls[0][0] == "snmpwalk"
    assert "-v1" in calls[0]
    assert "-Cr25" not in calls[0]


def test_device_metric_unit_is_limited_to_column_size() -> None:
    value = "Huawei-MA5800-V100R019-GPON_UNI 0/2/9 out"

    assert monitoring_metrics_service._device_metric_unit(value) == value[:40]
