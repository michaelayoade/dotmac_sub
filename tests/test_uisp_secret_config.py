"""UISP client config resolution follows the shared client conventions.

Token chain: UISP_API_TOKEN_FILE -> UISP_API_TOKEN (with bao:// reference
resolution) -> OpenBao uisp/api_token fallback. Base URL defaults to the
production controller. The reachability breaker fast-fails after transport
failures.
"""

from __future__ import annotations

from unittest.mock import Mock

from app.services import uisp


def test_uisp_token_resolves_secret_reference(monkeypatch):
    monkeypatch.delenv("UISP_API_TOKEN_FILE", raising=False)
    monkeypatch.setenv("UISP_API_TOKEN", "bao://secret/uisp#api_token")
    monkeypatch.setattr(
        "app.services.secrets.get_secret",
        Mock(side_effect=AssertionError("OpenBao fallback should not be probed")),
    )
    monkeypatch.setattr(
        "app.services.secrets.resolve_secret",
        lambda value: (
            "resolved-token" if value == "bao://secret/uisp#api_token" else value
        ),
    )

    assert uisp.get_uisp_api_token() == "resolved-token"


def test_uisp_token_prefers_env_over_openbao_fallback(monkeypatch):
    monkeypatch.delenv("UISP_API_TOKEN_FILE", raising=False)
    monkeypatch.setenv("UISP_API_TOKEN", "env-token")
    monkeypatch.setattr(
        "app.services.secrets.get_secret",
        Mock(side_effect=AssertionError("OpenBao fallback should not be probed")),
    )

    assert uisp.get_uisp_api_token() == "env-token"


def test_uisp_token_file_takes_precedence(monkeypatch, tmp_path):
    token_file = tmp_path / "uisp-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("UISP_API_TOKEN", "env-token")
    monkeypatch.setattr(
        "app.services.secrets.get_secret",
        Mock(side_effect=AssertionError("OpenBao fallback should not be probed")),
    )
    monkeypatch.setenv("UISP_API_TOKEN_FILE", str(token_file))

    assert uisp.get_uisp_api_token() == "file-token"


def test_uisp_token_uses_openbao_fallback(monkeypatch):
    monkeypatch.delenv("UISP_API_TOKEN", raising=False)
    monkeypatch.delenv("UISP_API_TOKEN_FILE", raising=False)
    monkeypatch.setattr(
        "app.services.secrets.get_secret",
        lambda path, field, default="": (
            "bao-token" if (path, field) == ("uisp", "api_token") else default
        ),
    )

    assert uisp.get_uisp_api_token() == "bao-token"


def test_uisp_url_defaults_to_production_controller(monkeypatch):
    monkeypatch.delenv("UISP_API_URL", raising=False)

    assert uisp.get_uisp_api_url() == "https://uisp.dotmac.ng"


def test_uisp_url_env_override_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("UISP_API_URL", "https://uisp.example.test/")

    assert uisp.get_uisp_api_url() == "https://uisp.example.test"


def test_uisp_configured_requires_token(monkeypatch):
    monkeypatch.setattr(uisp, "get_uisp_api_url", lambda: "https://uisp.example.test")
    monkeypatch.setattr(uisp, "get_uisp_api_token", lambda: "")

    assert uisp.uisp_configured() is False


def test_uisp_configured_with_resolved_token(monkeypatch):
    monkeypatch.setattr(uisp, "get_uisp_api_url", lambda: "https://uisp.example.test")
    monkeypatch.setattr(uisp, "get_uisp_api_token", lambda: "resolved-token")

    assert uisp.uisp_configured() is True


def test_uisp_reachability_circuit_starts_closed():
    circuit = uisp._UispReachabilityCircuit()
    assert circuit.is_open() is False


def test_uisp_reachability_circuit_opens_after_trip():
    circuit = uisp._UispReachabilityCircuit()
    circuit.trip()
    assert circuit.is_open() is True


def test_uisp_client_requires_configuration():
    try:
        uisp.UispClient(api_url="", api_token="token")
    except uisp.UispConfigurationError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected UispConfigurationError")

    try:
        uisp.UispClient(api_url="https://uisp.example.test", api_token="")
    except uisp.UispConfigurationError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected UispConfigurationError")


def test_uisp_client_exposes_scoped_configuration_write_helpers():
    public = {
        name
        for name in dir(uisp.UispClient)
        if not name.startswith("_") and callable(getattr(uisp.UispClient, name))
    }
    assert public == {
        "from_env",
        "list_devices",
        "list_sites",
        "list_airmax_stations",
        "list_olt_onus",
        "list_data_links",
        "get_device_configuration",
        "put_device_configuration",
    }


def test_uisp_client_uses_airos_configuration_routes(monkeypatch):
    client = uisp.UispClient("https://uisp.example.test", "token")
    calls = []
    monkeypatch.setattr(client, "_get", lambda path: calls.append(("GET", path)) or {})
    monkeypatch.setattr(
        client,
        "_put",
        lambda path, payload: calls.append(("PUT", path, payload)) or {},
    )

    client.get_device_configuration("device-1", transport="airos")
    client.put_device_configuration("device-1", {"wireless": {}}, transport="airos")

    assert calls == [
        ("GET", "/devices/airos/device-1/configuration"),
        ("PUT", "/devices/airos/device-1/configuration", {"wireless": {}}),
    ]


def test_uisp_client_uses_onu_bulk_configuration_routes(monkeypatch):
    client = uisp.UispClient("https://uisp.example.test", "token")
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        if path.endswith("get-configuration"):
            return [{"deviceId": "device-1", "wireless": {}}]
        return [{"deviceId": "device-1", "status": "success"}]

    monkeypatch.setattr(client, "_post", fake_post)

    observed = client.get_device_configuration("device-1", transport="onu")
    response = client.put_device_configuration("device-1", observed, transport="onu")

    assert calls == [
        ("/devices/get-configuration", {"deviceIds": ["device-1"]}),
        (
            "/devices/update-configuration",
            [{"deviceId": "device-1", "wireless": {}}],
        ),
    ]
    assert response == {"results": [{"deviceId": "device-1", "status": "success"}]}
