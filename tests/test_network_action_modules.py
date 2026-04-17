"""Smoke tests for the modular ONT/OLT action layout."""

from types import SimpleNamespace

from app.services.network import ont_action_network
from app.services.network.ont_actions import OntActions


def test_ont_actions_facade_exposes_split_methods() -> None:
    assert callable(OntActions.reboot)
    assert callable(OntActions.get_running_config)
    assert callable(OntActions.set_wifi_ssid)
    assert callable(OntActions.set_wifi_config)
    assert callable(OntActions.set_pppoe_credentials)
    assert callable(OntActions.configure_wan_config)
    assert callable(OntActions.set_lan_config)
    assert callable(OntActions.run_ping_diagnostic)


def test_set_lan_config_pushes_detected_tr181_paths(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    refresh_calls: list[tuple[str, str, bool]] = []
    cache: dict[str, str] = {}

    class FakeClient:
        def set_parameter_values(
            self,
            device_id: str,
            params: dict[str, str],
            *,
            connection_request: bool = True,
        ):
            calls.append((device_id, params))
            cache.update(params)
            return {"queued": True, "params": params}

        def get_parameter_values(
            self,
            _device_id: str,
            _paths: list[str],
            *,
            connection_request: bool = True,
        ):
            return {"queued": True}

        def get_device(self, _device_id: str):
            doc: dict = {}
            for p, v in cache.items():
                n = doc
                parts = p.split(".")
                for part in parts[:-1]:
                    n = n.setdefault(part, {})
                n[parts[-1]] = {"_value": v, "_timestamp": "now"}
            return doc

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


def test_set_lan_config_pushes_dhcp_server_range(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    cache: dict[str, str] = {}

    class FakeClient:
        def set_parameter_values(
            self,
            device_id: str,
            params: dict[str, str],
            *,
            connection_request: bool = True,
        ):
            calls.append((device_id, params))
            cache.update(params)
            return {"queued": True, "params": params}

        def get_parameter_values(
            self,
            _device_id: str,
            _paths: list[str],
            *,
            connection_request: bool = True,
        ):
            return {"queued": True}

        def get_device(self, _device_id: str):
            doc: dict = {}
            for p, v in cache.items():
                n = doc
                parts = p.split(".")
                for part in parts[:-1]:
                    n = n.setdefault(part, {})
                n[parts[-1]] = {"_value": v, "_timestamp": "now"}
            return doc

        def refresh_object(self, *_args, **_kwargs):
            return {"refreshed": True}

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
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )
    monkeypatch.setattr(ont_action_network, "persist_data_model_root", lambda *_: None)

    result = ont_action_network.set_lan_config(
        None,
        "ont-1",
        dhcp_enabled=True,
        dhcp_start="192.168.10.10",
        dhcp_end="192.168.10.200",
    )

    assert result.success is True
    assert calls == [
        (
            "device-1",
            {
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable": "true",
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MinAddress": "192.168.10.10",
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MaxAddress": "192.168.10.200",
            },
        )
    ]


def test_configure_wan_config_pushes_static_igd_paths(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    cache: dict[str, str] = {}

    class FakeClient:
        def set_parameter_values(
            self,
            device_id: str,
            params: dict[str, str],
            *,
            connection_request: bool = True,
        ):
            calls.append((device_id, params))
            cache.update(params)
            return {"queued": True}

        def get_parameter_values(
            self,
            _device_id: str,
            _paths: list[str],
            *,
            connection_request: bool = True,
        ):
            return {"queued": True}

        def get_device(self, _device_id: str):
            doc: dict = {}
            for p, v in cache.items():
                n = doc
                parts = p.split(".")
                for part in parts[:-1]:
                    n = n.setdefault(part, {})
                n[parts[-1]] = {"_value": v, "_timestamp": "now"}
            return doc

        def refresh_object(self, *_args, **_kwargs):
            return {"refreshed": True}

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
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )
    monkeypatch.setattr(ont_action_network, "persist_data_model_root", lambda *_: None)

    result = ont_action_network.configure_wan_config(
        None,
        "ont-1",
        wan_mode="static",
        wan_vlan=203,
        ip_address="172.16.203.50",
        subnet_mask="255.255.255.0",
        gateway="172.16.203.1",
        dns_servers="8.8.8.8,1.1.1.1",
    )

    assert result.success is True
    assert calls == [
        (
            "device-1",
            {
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.Enable": "1",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ConnectionType": "IP_Routed",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.AddressingType": "Static",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress": "172.16.203.50",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.SubnetMask": "255.255.255.0",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.DefaultGateway": "172.16.203.1",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.DNSServers": "8.8.8.8,1.1.1.1",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.X_HW_VLAN": "203",
            },
        )
    ]


def test_configure_wan_config_refuses_pppoe_add_object_on_management_wan(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    add_object_calls: list[tuple[str, str]] = []
    cache: dict[str, str | int] = {
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
            "WANIPConnectionNumberOfEntries"
        ): 1,
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
            "WANPPPConnectionNumberOfEntries"
        ): 0,
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
            "WANIPConnection.1.Name"
        ): "OLT_C_TR069_Static_WAN",
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
            "WANIPConnection.1.X_HW_SERVICELIST"
        ): "TR069",
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
            "WANIPConnection.1.X_HW_VLAN"
        ): 201,
    }

    class FakeClient:
        def extract_parameter_value(self, device: dict, parameter_path: str):
            current = device
            for part in parameter_path.split("."):
                if not isinstance(current, dict):
                    return None
                current = current.get(part)
                if current is None:
                    return None
            if isinstance(current, dict) and "_value" in current:
                return current["_value"]
            return None if isinstance(current, dict) else current

        def get_device(self, _device_id: str):
            doc: dict = {}
            for path, value in cache.items():
                node = doc
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = {"_value": value, "_timestamp": "now"}
            return doc

        def set_parameter_values(
            self,
            device_id: str,
            params: dict[str, str],
            **_kwargs,
        ):
            calls.append((device_id, params))
            cache.update(params)
            return {"queued": True}

        def get_parameter_values(self, *_args, **_kwargs):
            return {"queued": True}

        def add_object(self, device_id: str, object_path: str, **_kwargs):
            add_object_calls.append((device_id, object_path))
            raise AssertionError("management WAN must not be mutated")

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
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )
    monkeypatch.setattr(ont_action_network, "persist_data_model_root", lambda *_: None)

    result = ont_action_network.configure_wan_config(
        None,
        "ont-1",
        wan_mode="pppoe",
        wan_vlan=203,
        instance_index=1,
    )

    assert result.success is False
    assert "Refusing to create one" in result.message
    assert calls == []
    assert add_object_calls == []


def test_set_pppoe_credentials_accepts_precreated_ppp_wan_with_ppp_vlan(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    cache: dict[str, str | int] = {
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
            "WANIPConnectionNumberOfEntries"
        ): 0,
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
            "WANPPPConnectionNumberOfEntries"
        ): 1,
        (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
            "WANPPPConnection.1.X_HW_VLAN"
        ): 203,
    }

    class FakeClient:
        def extract_parameter_value(self, device: dict, parameter_path: str):
            current = device
            for part in parameter_path.split("."):
                if not isinstance(current, dict):
                    return None
                current = current.get(part)
                if current is None:
                    return None
            if isinstance(current, dict) and "_value" in current:
                return current["_value"]
            return None if isinstance(current, dict) else current

        def get_device(self, _device_id: str):
            doc: dict = {}
            for path, value in cache.items():
                node = doc
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = {"_value": value, "_timestamp": "now"}
            return doc

        def set_parameter_values(
            self,
            device_id: str,
            params: dict[str, str],
            *,
            connection_request: bool = True,
        ):
            calls.append((device_id, params))
            cache.update(params)
            return {"queued": True}

        def get_parameter_values(self, *_args, **_kwargs):
            return {"queued": True}

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
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )
    monkeypatch.setattr(ont_action_network, "persist_data_model_root", lambda *_: None)

    result = ont_action_network.set_pppoe_credentials(
        None,
        "ont-1",
        "100008817",
        "secret",
        wan_vlan=203,
        instance_index=2,
    )

    assert result.success is True
    assert calls == [
        (
            "device-1",
            {
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection.1.Username": "100008817",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection.1.Password": "secret",
            },
        )
    ]


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
