from types import SimpleNamespace

from app.services import web_network_core_devices_views as core_devices_views
from app.services.genieacs import GenieACSError
from app.services.network import ont_action_wifi


def test_set_wifi_password_falls_back_to_supported_path(monkeypatch) -> None:
    attempts: list[str] = []
    refresh_calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def set_parameter_values(self, device_id: str, params: dict[str, str]):
            path = next(iter(params))
            attempts.append(path)
            if path.endswith("WLANConfiguration.1.KeyPassphrase"):
                return {"device_id": device_id, "path": path}
            raise GenieACSError("invalid parameter name")

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
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase",
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.KeyPassphrase",
    ]
    assert refresh_calls == [("device-1", "InternetGatewayDevice.", True)]


def test_set_wifi_config_pushes_radio_ssid_channel_and_security(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeClient:
        def set_parameter_values(self, device_id: str, params: dict[str, str]):
            calls.append((device_id, params))
            return {"queued": True}

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
        (
            "device-1",
            {
                "Device.WiFi.SSID.1.Enable": "true",
                "Device.WiFi.SSID.1.SSID": "Customer WiFi",
                "Device.WiFi.Radio.1.Channel": "6",
                "Device.WiFi.AccessPoint.1.Security.ModeEnabled": "WPA2-Personal",
            },
        )
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
