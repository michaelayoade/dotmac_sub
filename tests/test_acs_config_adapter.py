from __future__ import annotations


def test_acs_config_adapter_delegates_wifi_config(monkeypatch) -> None:
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.network import ont_action_wifi
    from app.services.network.ont_action_common import ActionResult

    calls = {}

    def fake_set_wifi_config(db, ont_id, **kwargs):
        calls["db"] = db
        calls["ont_id"] = ont_id
        calls["kwargs"] = kwargs
        return ActionResult(success=True, message="ok", data={"adapter": "acs"})

    monkeypatch.setattr(ont_action_wifi, "set_wifi_config", fake_set_wifi_config)

    result = acs_config_adapter.set_wifi_config(
        object(),
        "ont-1",
        enabled=True,
        ssid="DOTMAC-1001",
        password="Secret123",
        channel=6,
        security_mode="WPA2-Personal",
    )

    assert result.success is True
    assert calls["ont_id"] == "ont-1"
    assert calls["kwargs"] == {
        "enabled": True,
        "ssid": "DOTMAC-1001",
        "password": "Secret123",
        "channel": 6,
        "security_mode": "WPA2-Personal",
    }


def test_acs_config_adapter_delegates_wan_config(monkeypatch) -> None:
    from app.services.acs_config_adapter import acs_config_adapter
    from app.services.network import ont_action_network
    from app.services.network.ont_action_common import ActionResult

    calls = {}

    def fake_configure_wan_config(db, ont_id, **kwargs):
        calls["db"] = db
        calls["ont_id"] = ont_id
        calls["kwargs"] = kwargs
        return ActionResult(success=True, message="ok", data={"adapter": "acs"})

    monkeypatch.setattr(
        ont_action_network,
        "configure_wan_config",
        fake_configure_wan_config,
    )

    result = acs_config_adapter.configure_wan_config(
        object(),
        "ont-1",
        wan_mode="static",
        wan_vlan=203,
        ip_address="100.64.1.20",
        subnet_mask="255.255.255.0",
        gateway="100.64.1.1",
        dns_servers="1.1.1.1",
        instance_index=2,
    )

    assert result.success is True
    assert calls["ont_id"] == "ont-1"
    assert calls["kwargs"] == {
        "wan_mode": "static",
        "wan_vlan": 203,
        "ip_address": "100.64.1.20",
        "subnet_mask": "255.255.255.0",
        "gateway": "100.64.1.1",
        "dns_servers": "1.1.1.1",
        "instance_index": 2,
    }
