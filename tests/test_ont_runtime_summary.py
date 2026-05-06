from datetime import UTC, datetime
from types import SimpleNamespace

from app.services import web_network_core_devices_views as views


def test_observed_runtime_summary_only_lists_present_fields(monkeypatch):
    monkeypatch.setattr(
        views,
        "resolve_effective_ont_config",
        lambda _db, _ont: {"values": {"pppoe_username": None, "wan_mode": "pppoe"}},
    )
    ont = SimpleNamespace(
        mac_address=None,
        observed_wan_ip=None,
        observed_pppoe_status=None,
        observed_wifi_clients=0,
        observed_lan_hosts=4,
    )
    summary = views._acs_observed_runtime_summary(
        {
            "available": True,
            "fetched_at": datetime(2026, 5, 4, 14, 27, tzinfo=UTC),
            "observed": {
                "lan_hosts": [
                    {"host_name": "phone", "ip_address": "192.168.100.10", "active": True},
                    {"host_name": "old-phone", "ip_address": "192.168.100.11", "active": False},
                ]
            },
            "tracked_point_index": {},
        },
        db=None,
        ont=ont,
    )

    fields = {field["label"]: field["value"] for field in summary["runtime_fields"]}
    assert "WAN IP" not in fields
    assert "PPPoE Status" not in fields
    assert fields["WiFi Clients"] == 0
    assert fields["Active Devices"] == 1
    assert fields["Known Devices"] == 2


def test_observed_runtime_summary_uses_persisted_runtime_fallbacks(monkeypatch):
    monkeypatch.setattr(
        views,
        "resolve_effective_ont_config",
        lambda _db, _ont: {"values": {"pppoe_username": "user-123", "wan_mode": "pppoe"}},
    )
    ont = SimpleNamespace(
        mac_address=None,
        observed_wan_ip="172.16.141.59",
        observed_pppoe_status="Connected",
        observed_wifi_clients=None,
        observed_lan_hosts=None,
    )
    summary = views._acs_observed_runtime_summary(
        {"available": True, "observed": {}, "tracked_point_index": {}},
        db=None,
        ont=ont,
    )

    fields = {field["label"]: field["value"] for field in summary["runtime_fields"]}
    assert fields["WAN IP"] == "172.16.141.59"
    assert fields["PPPoE User"] == "user-123"
    assert fields["PPPoE Status"] == "Connected"
