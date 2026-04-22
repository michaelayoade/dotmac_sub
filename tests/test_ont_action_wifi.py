from types import SimpleNamespace

from app.services import web_network_core_devices_views as core_devices_views
from app.services.genieacs import GenieACSError
from app.services.network import ont_action_wifi


def test_normalize_security_mode_maps_tr181_names_to_tr098_beacon_type() -> None:
    """UI sends TR-181-style names but TR-098 BeaconType has different vocab."""
    n = ont_action_wifi._normalize_security_mode
    # Common UI inputs → TR-098 native values
    assert n("WPA2-Personal", "InternetGatewayDevice") == "11i"
    assert n("wpa2-personal", "InternetGatewayDevice") == "11i"
    assert n("WPA-WPA2-Personal", "InternetGatewayDevice") == "WPAand11i"
    assert n("Mixed", "InternetGatewayDevice") == "WPAand11i"
    assert n("None", "InternetGatewayDevice") == "None"
    # TR-098 native values pass through unchanged
    assert n("11i", "InternetGatewayDevice") == "11i"
    assert n("WPAand11i", "InternetGatewayDevice") == "WPAand11i"
    # TR-181 values pass through unchanged on Device root
    assert n("WPA2-Personal", "Device") == "WPA2-Personal"
    assert n("WPA-WPA2-Personal", "Device") == "WPA-WPA2-Personal"
    # Unknown values fall through (operator can pass an exact device value)
    assert n("SomeCustomMode", "InternetGatewayDevice") == "SomeCustomMode"


def test_set_wifi_config_translates_security_mode_for_tr098_device(monkeypatch) -> None:
    """Regression: BeaconType should receive the device-native value, not the raw UI string."""
    calls: list[dict[str, str]] = []
    cache: dict[str, str] = {}

    class FakeClient:
        def set_parameter_values(self, _device_id, params):
            calls.append(dict(params))
            cache.update(params)
            return {"queued": True}

        def get_parameter_values(self, _device_id, _paths):
            return {"queued": True}

        def get_device(self, _device_id):
            doc: dict = {}
            for path, value in cache.items():
                node = doc
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = {"_value": value, "_timestamp": "now"}
            return doc

        def refresh_object(self, *_args, **_kwargs):
            return {"refreshed": True}

    monkeypatch.setattr(
        ont_action_wifi,
        "get_ont_client_or_error",
        lambda _db, _ont_id: (
            (SimpleNamespace(serial_number="ONT-1"), FakeClient(), "device-1"),
            None,
        ),
    )
    monkeypatch.setattr(
        ont_action_wifi,
        "detect_data_model_root",
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )

    result = ont_action_wifi.set_wifi_config(
        None,
        "ont-1",
        security_mode="WPA2-Personal",
    )

    assert result.success is True
    assert len(calls) == 1
    sent = calls[0]
    assert (
        sent["InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType"]
        == "11i"
    )


def test_set_wifi_password_falls_back_to_supported_path(monkeypatch) -> None:
    attempts: list[str] = []
    refresh_calls: list[tuple[str, str, bool]] = []
    # Simulated GenieACS device cache — updated by successful set_parameter_values.
    cache: dict[str, str] = {}

    class FakeClient:
        def set_parameter_values(
            self,
            device_id: str,
            params: dict[str, str],
            *,
            connection_request: bool = True,
        ):
            path = next(iter(params))
            attempts.append(path)
            if path.endswith("WLANConfiguration.1.KeyPassphrase"):
                cache[path] = params[path]
                return {"device_id": device_id, "path": path}
            raise GenieACSError("invalid parameter name")

        def get_parameter_values(self, _device_id: str, _paths: list[str]):
            return {"queued": True}

        def get_device(self, _device_id: str):
            # Build a minimal nested dict from the simulated cache for the paths
            # _read_param_from_cache walks.
            doc: dict = {}
            for path, value in cache.items():
                node = doc
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = {"_value": value, "_timestamp": "now"}
            return doc

        def refresh_object(self, device_id: str, path: str):
            refresh_calls.append((device_id, path))
            return {"refreshed": path}

    monkeypatch.setattr(
        ont_action_wifi,
        "get_ont_client_or_error",
        lambda _db, _ont_id: (
            (SimpleNamespace(serial_number="ONT-1"), FakeClient(), "device-1"),
            None,
        ),
    )
    monkeypatch.setattr(
        ont_action_wifi,
        "detect_data_model_root",
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )

    result = ont_action_wifi.set_wifi_password(None, "ont-1", "SuperSecret123")

    assert result.success is True
    assert attempts == [
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey",
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.KeyPassphrase",
    ]
    assert refresh_calls == [
        ("device-1", "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.")
    ]


def test_set_wifi_password_fails_when_device_cache_does_not_confirm(
    monkeypatch,
) -> None:
    """Regression: set_parameter_values returning 200 is not proof of applied value.

    If GenieACS accepts the task but the device cache never reflects the target
    value (observed on some Huawei ONTs that silently ignore the SPV), the UI
    used to report success. Post-fix, the function must raise so the caller
    returns an actionable failure instead of a misleading success.
    """

    class FakeClient:
        def set_parameter_values(
            self,
            device_id: str,
            _params: dict[str, str],
            *,
            connection_request: bool = True,
        ):
            # Accept the task on every candidate but never update the cache.
            return {"device_id": device_id, "accepted": True}

        def get_parameter_values(
            self,
            _device_id: str,
            _paths: list[str],
            *,
            connection_request: bool = True,
        ):
            return {"queued": True}

        def get_device(self, _device_id: str):
            return {}

        def refresh_object(self, *_args, **_kwargs):
            return {"refreshed": True}

    monkeypatch.setattr(
        ont_action_wifi,
        "get_ont_client_or_error",
        lambda _db, _ont_id: (
            (SimpleNamespace(serial_number="ONT-1"), FakeClient(), "device-1"),
            None,
        ),
    )
    monkeypatch.setattr(
        ont_action_wifi,
        "detect_data_model_root",
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )

    result = ont_action_wifi.set_wifi_password(None, "ont-1", "NewPass9999")

    assert result.success is False
    assert "not applied" in result.message.lower() or "failed" in result.message.lower()


def test_set_wifi_config_pushes_radio_ssid_channel_and_security(monkeypatch) -> None:
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
            for path, value in cache.items():
                node = doc
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = {"_value": value, "_timestamp": "now"}
            return doc

        def refresh_object(self, *_args, **_kwargs):
            return {"refreshed": True}

    monkeypatch.setattr(
        ont_action_wifi,
        "get_ont_client_or_error",
        lambda _db, _ont_id: (
            (SimpleNamespace(serial_number="ONT-1"), FakeClient(), "device-1"),
            None,
        ),
    )
    monkeypatch.setattr(
        ont_action_wifi,
        "detect_data_model_root",
        lambda _db, _ont, _client, _device_id: "Device",
    )

    result = ont_action_wifi.set_wifi_config(
        None,
        "ont-1",
        enabled=True,
        ssid="Customer WiFi",
        channel=6,
        security_mode="WPA2-Personal",
    )

    assert result.success is True
    assert calls == [
        ("device-1", {"Device.WiFi.SSID.1.SSID": "Customer WiFi"}),
        ("device-1", {"Device.WiFi.SSID.1.Enable": "true"}),
        ("device-1", {"Device.WiFi.Radio.1.Channel": "6"}),
        (
            "device-1",
            {"Device.WiFi.AccessPoint.1.Security.ModeEnabled": "WPA2-Personal"},
        ),
    ]


def test_set_wifi_config_keeps_ssid_strict_but_tolerates_omitted_optional_readback(
    monkeypatch,
) -> None:
    calls: list[dict[str, str]] = []
    cache: dict[str, str] = {
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID": "wifi"
    }

    class FakeClient:
        def set_parameter_values(
            self,
            _device_id: str,
            params: dict[str, str],
            *,
            connection_request: bool = True,
        ):
            calls.append(dict(params))
            for path, value in params.items():
                if path.endswith(".SSID"):
                    cache[path] = value
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
            for path, value in cache.items():
                node = doc
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = {"_value": value, "_timestamp": "now"}
            return doc

        def refresh_object(self, *_args, **_kwargs):
            return {"refreshed": True}

    monkeypatch.setattr(
        ont_action_wifi,
        "get_ont_client_or_error",
        lambda _db, _ont_id: (
            (SimpleNamespace(serial_number="HWTT20A7B0A9"), FakeClient(), "device-1"),
            None,
        ),
    )
    monkeypatch.setattr(
        ont_action_wifi,
        "detect_data_model_root",
        lambda _db, _ont, _client, _device_id: "InternetGatewayDevice",
    )

    result = ont_action_wifi.set_wifi_config(
        None,
        "ont-1",
        enabled=True,
        ssid="The Residence",
        security_mode="WPA-WPA2-Personal",
    )

    assert result.success is True
    assert cache["InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID"] == (
        "The Residence"
    )
    assert calls == [
        {
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID": (
                "The Residence"
            )
        },
        {"InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable": "true"},
        {"InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable": "true"},
        {
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType": (
                "WPAand11i"
            )
        },
        {
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType": (
                "WPAand11i"
            )
        },
    ]


def test_normalize_port_name_uses_canonical_pon_hint() -> None:
    assert core_devices_views._normalize_port_name("GPON 0/1/0") == "0/1/0"
    assert core_devices_views._normalize_port_name("0/1/0") == "0/1/0"


def test_dedupe_live_board_inventory_collapses_duplicate_slots() -> None:
    deduped = core_devices_views._dedupe_live_board_inventory(
        [
            {
                "index": "101",
                "slot_number": 1,
                "card_type": "Control Board",
                "category": "card",
            },
            {
                "index": "202",
                "slot_number": 1,
                "card_type": "Main Control Board H901MPLA",
                "category": "card",
            },
            {
                "index": "303",
                "slot_number": 2,
                "card_type": "GPON Service Board",
                "category": "card",
            },
        ]
    )

    assert len(deduped) == 2
    assert deduped[0]["slot_number"] == 1
    assert deduped[0]["card_type"] == "Main Control Board H901MPLA"
    assert deduped[1]["slot_number"] == 2
