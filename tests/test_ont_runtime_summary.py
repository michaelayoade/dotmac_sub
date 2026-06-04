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
                    {
                        "host_name": "phone",
                        "ip_address": "192.168.100.10",
                        "active": True,
                    },
                    {
                        "host_name": "old-phone",
                        "ip_address": "192.168.100.11",
                        "active": False,
                    },
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
        lambda _db, _ont: {
            "values": {"pppoe_username": "user-123", "wan_mode": "pppoe"}
        },
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


def test_build_ont_provisioning_summary_for_pending_acs_registration() -> None:
    ont = SimpleNamespace(id="ont-1", provisioning_status="pending_acs_registration")
    events = [
        SimpleNamespace(
            step_name="authorization_baseline",
            status="waiting",
            message="Authorization baseline applied; waiting for ACS bootstrap verification.",
            created_at=datetime(2026, 5, 25, 14, 30, tzinfo=UTC),
            event_data={
                "domain_outcomes": {
                    "config_pack_resolution": {
                        "status": "succeeded",
                        "message": "Config pack resolved.",
                    },
                    "olt_l2_apply": {
                        "status": "succeeded",
                        "message": "Internet L2 apply completed.",
                    },
                    "management_path_apply": {
                        "status": "succeeded",
                        "message": "Management path apply completed.",
                    },
                    "acs_bootstrap_verify": {
                        "status": "pending_verification",
                        "message": "Waiting for ACS bootstrap verification after baseline apply.",
                    },
                }
            },
        )
    ]

    summary = views._build_ont_provisioning_summary(ont, events)

    assert summary["headline"] == "Waiting for ACS registration"
    assert summary["status"] == "pending_acs_registration"
    assert summary["last_event_label"] == "Authorization baseline"
    assert summary["attention_items"][0]["label"] == "ACS registration"
    domain_rows = {row["key"]: row for row in summary["domain_rows"]}
    assert domain_rows["config_pack_resolution"]["status_label"] == "Completed"
    assert domain_rows["acs_bootstrap_verify"]["status_label"] == "Waiting"


def test_build_ont_provisioning_summary_surfaces_failure_class() -> None:
    ont = SimpleNamespace(id="ont-2", provisioning_status="failed")
    events = [
        SimpleNamespace(
            step_name="resolve_effective_config_pack",
            status="failed",
            message="Config-pack incomplete: management VLAN is missing.",
            created_at=datetime(2026, 5, 25, 14, 35, tzinfo=UTC),
            event_data={
                "failure_class": "config_pack_incomplete",
                "domain_outcomes": {
                    "config_pack_resolution": {
                        "status": "terminal_failure",
                        "message": "Config-pack incomplete: management VLAN is missing.",
                    }
                },
            },
        )
    ]

    summary = views._build_ont_provisioning_summary(ont, events)

    assert summary["headline"] == "Provisioning failed"
    assert summary["failure_class_code"] == "config_pack_incomplete"
    assert summary["failure_class_label"] == "Config pack incomplete"
    assert summary["attention_items"][0]["label"] == "Config pack"
