from __future__ import annotations

from app.services.network.ont_action_common import ActionResult
from app.services.web_network_ont_actions import config_setters


class _FakeClient:
    def get_device(self, _device_id: str) -> dict[str, object]:
        return {
            "InternetGatewayDevice": {
                "WANDevice": {
                    "1": {
                        "WANConnectionDevice": {
                            "1": {
                                "WANIPConnection": {
                                    "1": {"ExternalIPAddress": "172.16.201.78"}
                                },
                                "WANPPPConnection": {},
                            }
                        }
                    }
                }
            }
        }


def test_bind_internet_wan_falls_back_to_huawei_policy_route(monkeypatch) -> None:
    from app.services.network import ont_action_common

    captured: dict[str, object] = {}

    def fake_resolver(
        _db: object, _ont_id: str
    ) -> tuple[tuple[object, object, str], None]:
        return (object(), _FakeClient(), "device-id"), None

    def fake_policy_route(
        _db: object,
        ont_id: str,
        *,
        requested_binds: dict[str, bool],
        request: object | None,
    ) -> ActionResult:
        captured["ont_id"] = ont_id
        captured["requested_binds"] = requested_binds
        captured["request"] = request
        return ActionResult(success=True, message="policy route ok")

    monkeypatch.setattr(ont_action_common, "get_ont_client_or_error", fake_resolver)
    monkeypatch.setattr(
        config_setters,
        "_bind_internet_wan_via_olt_policy_route",
        fake_policy_route,
    )

    result = config_setters.bind_internet_wan(
        object(),
        "ont-1",
        ssid1=True,
        lan1=True,
        lan2=True,
        lan3=True,
        lan4=True,
    )

    assert result.success is True
    assert captured["ont_id"] == "ont-1"
    assert captured["requested_binds"] == {
        "Lan1Enable": True,
        "Lan2Enable": True,
        "Lan3Enable": True,
        "Lan4Enable": True,
        "SSID1Enable": True,
    }
