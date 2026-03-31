from app.services.network import ont_metrics
from app.services.network.olt_ssh import _DEFAULT_HUAWEI_PROMPT, _run_huawei_cmd
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
    assert 'ont_serial=~"^(?:' in selector
    assert r"HWTC\-7D4510C3" not in selector
    assert "HWTC-7D4510C3" in selector


def test_huawei_command_defaults_accept_user_and_exec_prompts(monkeypatch) -> None:
    channel = _DummyChannel()

    def _fake_read_until_prompt(_channel, prompt_regex: str, timeout_sec: float = 8.0) -> str:
        assert prompt_regex.startswith(_DEFAULT_HUAWEI_PROMPT)
        assert timeout_sec == 12
        return "OLT>"

    monkeypatch.setattr("app.services.network.olt_ssh._read_until_prompt", _fake_read_until_prompt)

    output = _run_huawei_cmd(channel, "display version")

    assert output == "OLT>"
    assert channel.sent == ["display version\n"]


def test_olt_autofind_get_route_exists_for_auth_redirect_recovery() -> None:
    route = next(
        r
        for r in olts_inventory_router.routes
        if getattr(r, "path", None) == "/network/olts/{olt_id}/autofind"
        and "GET" in getattr(r, "methods", set())
    )
    assert "GET" in route.methods
