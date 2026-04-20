from types import SimpleNamespace

from app.services.web_network_ont_actions.context_builders import (
    _desired_config_context,
)


def test_desired_config_context_prefers_durable_ont_fields() -> None:
    ont = SimpleNamespace(
        mgmt_ip_mode=SimpleNamespace(value="static"),
        mgmt_vlan=SimpleNamespace(tag=300),
        mgmt_ip_address="10.30.0.44",
        wan_mode=SimpleNamespace(value="pppoe"),
        wan_vlan=SimpleNamespace(tag=203),
        pppoe_username="customer@example",
        lan_gateway_ip="192.168.44.1",
        lan_subnet_mask="255.255.255.0",
        lan_dhcp_enabled=True,
        lan_dhcp_start="192.168.44.20",
        lan_dhcp_end="192.168.44.200",
        wifi_enabled=True,
        wifi_ssid="CustomerWiFi",
        wifi_channel="6",
        wifi_security_mode="WPA2-Personal",
    )

    context = _desired_config_context(
        ont,
        ont_plan={
            "configure_management_ip": {
                "ip_mode": "dhcp",
                "vlan_id": 100,
                "ip_address": "10.0.0.10",
            },
            "configure_wan_tr069": {
                "wan_mode": "dhcp",
                "wan_vlan": 100,
            },
            "push_pppoe_tr069": {"username": "old@example"},
            "configure_lan_tr069": {
                "lan_ip": "192.168.1.1",
                "dhcp_enabled": False,
            },
            "configure_wifi_tr069": {
                "enabled": False,
                "ssid": "OldWiFi",
            },
        },
        initial_iphost_form={
            "ip_mode": "dhcp",
            "vlan_id": "99",
            "ip_address": "10.0.0.99",
        },
    )

    assert context["desired_mgmt_config"]["ip_mode"] == "static"
    assert context["desired_mgmt_config"]["vlan_id"] == "300"
    assert context["desired_mgmt_config"]["ip_address"] == "10.30.0.44"
    assert context["desired_wan_config"]["wan_mode"] == "pppoe"
    assert context["desired_wan_config"]["wan_vlan"] == "203"
    assert context["desired_wan_config"]["pppoe_username"] == "customer@example"
    assert context["desired_lan_config"]["lan_ip"] == "192.168.44.1"
    assert context["desired_lan_config"]["dhcp_enabled"] is True
    assert context["desired_wifi_config"]["enabled"] is True
    assert context["desired_wifi_config"]["ssid"] == "CustomerWiFi"


def test_unified_config_context_does_not_perform_live_reads(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services import service_intent_ui_adapter
    from app.services import web_network_onts as web_network_onts_service
    from app.services.web_network_ont_actions import context_builders

    ont = OntUnit(
        serial_number="DB-ONLY-CONFIG",
        pppoe_username="customer@example",
        wifi_ssid="CustomerWiFi",
        lan_gateway_ip="192.168.44.1",
    )
    db_session.add(ont)
    db_session.commit()

    def fail_live_read(*_args, **_kwargs):
        raise AssertionError("unified config overview must not perform live reads")

    monkeypatch.setattr(context_builders, "fetch_iphost_config_with_meta", fail_live_read)
    monkeypatch.setattr(
        web_network_onts_service,
        "get_tr069_profiles_for_ont_with_meta",
        fail_live_read,
    )
    monkeypatch.setattr(
        service_intent_ui_adapter.service_intent_ui_adapter,
        "load_acs_observed_service_intent",
        fail_live_read,
    )

    context = context_builders.unified_config_context(db_session, str(ont.id))

    assert context["desired_wan_config"]["pppoe_username"] == "customer@example"
    assert context["desired_wifi_config"]["ssid"] == "CustomerWiFi"
    assert context["desired_lan_config"]["lan_ip"] == "192.168.44.1"
    assert context["iphost_freshness"]["source"] == "db"
