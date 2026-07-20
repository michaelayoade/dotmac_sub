import pytest

from app.services.router_management.connection import (
    DANGEROUS_COMMANDS,
    RouterConnectionService,
    check_dangerous_commands,
)


def test_check_dangerous_commands_blocks_reset():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/system/reset-configuration"])


def test_check_dangerous_commands_blocks_shutdown():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/system/shutdown"])


def test_check_dangerous_commands_allows_safe():
    check_dangerous_commands(
        [
            "/queue simple set [find] queue=sfq/sfq",
            "/ip address add address=10.0.0.1/24 interface=ether1",
        ]
    )


def test_check_dangerous_commands_case_insensitive():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/System/Reset-Configuration"])


def test_build_base_url_ssl():
    url = RouterConnectionService._build_base_url(
        management_ip="10.0.0.1", port=443, use_ssl=True
    )
    assert url == "https://10.0.0.1:443"


def test_build_base_url_no_ssl():
    url = RouterConnectionService._build_base_url(
        management_ip="10.0.0.1", port=80, use_ssl=False
    )
    assert url == "http://10.0.0.1:80"


def test_dangerous_commands_list_is_not_empty():
    assert len(DANGEROUS_COMMANDS) >= 4


def test_execute_honors_tunable_overrides(monkeypatch):
    """Explicit connect/read/max_retries overrides beat the settings tunables."""
    from types import SimpleNamespace

    import httpx

    calls = {"n": 0}
    recorded = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, **kwargs):
            calls["n"] += 1
            raise httpx.ConnectError("boom")

    def fake_get_client(router, *, timeout=None):
        recorded["timeout"] = timeout
        return FakeClient()

    monkeypatch.setattr(
        "app.services.router_management.connection._rest_tunables",
        lambda: (10.0, 30.0, 3, 2.0),
    )
    monkeypatch.setattr(RouterConnectionService, "get_client", fake_get_client)

    router = SimpleNamespace(name="r1")
    with pytest.raises(RuntimeError, match="after 1 attempts"):
        RouterConnectionService.execute(
            router,
            "GET",
            "/ip/neighbor",
            connect_timeout=5.0,
            read_timeout=15.0,
            max_retries=1,
        )
    assert calls["n"] == 1  # single attempt, no retry sleep
    assert recorded["timeout"].connect == 5.0
    assert recorded["timeout"].read == 15.0
