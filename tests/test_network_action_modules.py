"""Smoke tests for the modular ONT/OLT action layout."""

from types import SimpleNamespace

from app.services.network import ont_action_network
from app.services.network.ont_actions import OntActions


def test_ont_actions_facade_exposes_split_methods() -> None:
    assert callable(OntActions.reboot)
    assert callable(OntActions.get_running_config)
    assert callable(OntActions.set_wifi_ssid)
    assert callable(OntActions.set_pppoe_credentials)
    assert callable(OntActions.set_lan_config)
    assert callable(OntActions.run_ping_diagnostic)


def test_set_lan_config_pushes_detected_tr181_paths(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    refresh_calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def set_parameter_values(self, device_id: str, params: dict[str, str]):
            calls.append((device_id, params))
            return {"queued": True, "params": params}

        def refresh_object(
            self,
            device_id: str,
            path: str,
            *,
            connection_request: bool = False,
        ):
            refresh_calls.append((device_id, path, connection_request))
            return {"refreshed": path}

    monkeypatch.setattr(
        ont_action_network,
        "get_ont_client_or_error",
        lambda _db, _ont_id: (
            (SimpleNamespace(serial_number="ONT-1"), FakeClient(), "device-1"),
            None,
        ),
    )
    monkeypatch.setattr(
        ont_action_network,
        "detect_data_model_root",
        lambda _db, _ont, _client, _device_id: "Device",
    )
    monkeypatch.setattr(ont_action_network, "persist_data_model_root", lambda *_: None)

    result = ont_action_network.set_lan_config(
        None,
        "ont-1",
        lan_ip="192.168.10.1",
        lan_subnet="255.255.255.0",
    )

    assert result.success is True
    assert calls == [
        (
            "device-1",
            {
                "Device.IP.Interface.2.IPv4Address.1.IPAddress": "192.168.10.1",
                "Device.IP.Interface.2.IPv4Address.1.SubnetMask": "255.255.255.0",
            },
        )
    ]
    assert refresh_calls == [("device-1", "Device.IP.Interface.2.", True)]


def test_set_lan_config_validates_input_before_resolving(monkeypatch) -> None:
    def _should_not_resolve(*_args, **_kwargs):
        raise AssertionError("resolver should not be called for invalid input")

    monkeypatch.setattr(
        ont_action_network,
        "get_ont_client_or_error",
        _should_not_resolve,
    )

    result = ont_action_network.set_lan_config(None, "ont-1", lan_ip="not-an-ip")

    assert result.success is False
    assert "valid IPv4 address" in result.message


def test_focused_olt_action_modules_importable() -> None:
    from app.services.network.olt_ssh_ont import (
        bind_tr069_server_profile,
        configure_ont_iphost,
    )
    from app.services.network.olt_ssh_profiles import (
        get_line_profiles,
        get_tr069_server_profiles,
    )
    from app.services.network.olt_ssh_service_ports import (
        create_single_service_port,
        delete_service_port,
        get_service_ports_for_ont,
    )

    assert callable(create_single_service_port)
    assert callable(delete_service_port)
    assert callable(get_service_ports_for_ont)
    assert callable(configure_ont_iphost)
    assert callable(bind_tr069_server_profile)
    assert callable(get_line_profiles)
    assert callable(get_tr069_server_profiles)
