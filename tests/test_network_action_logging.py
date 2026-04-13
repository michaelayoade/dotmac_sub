import logging

from app.services.network.action_logging import (
    log_network_action_result,
    looks_like_prerequisite_failure,
)
from app.services.web_network_onts_provisioning import validate_provision_form_fields


def test_prerequisite_failure_classifier_covers_olt_and_ont_messages():
    assert looks_like_prerequisite_failure("No TR-069 device linked")
    assert looks_like_prerequisite_failure("OLT not found")
    assert looks_like_prerequisite_failure("Missing ONT selection or target profile")
    assert looks_like_prerequisite_failure("SNMP test failed: no response from device")
    assert not looks_like_prerequisite_failure("Firmware upgrade command rejected")


def test_log_network_action_result_emits_structured_error(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.services.network.action_logging.web_admin_service.get_current_user",
        lambda _request: {"email": "operator@example.test"},
    )

    caplog.set_level(logging.ERROR, logger="app.services.network.action_logging")
    log_network_action_result(
        request=object(),
        resource_type="olt",
        resource_id="olt-123",
        action="Test SSH Connection",
        success=False,
        message="OLT not found",
        metadata={"source": "unit-test"},
    )

    record = next(
        item
        for item in caplog.records
        if item.getMessage().startswith(
            "Network action blocked by missing prerequisite"
        )
    )
    assert record.event == "network_action_prerequisite_blocked"
    assert record.network_resource_type == "olt"
    assert record.network_resource_id == "olt-123"
    assert record.network_action == "Test SSH Connection"
    assert record.actor == "operator@example.test"
    assert record.reason == "OLT not found"
    assert record.metadata == {"source": "unit-test"}


def test_log_network_action_result_ignores_successes(caplog):
    caplog.set_level(logging.ERROR, logger="app.services.network.action_logging")
    log_network_action_result(
        request=None,
        resource_type="ont",
        resource_id="ont-123",
        action="Refresh ONT",
        success=True,
        message="OK",
    )

    assert not caplog.records


def test_validate_provision_form_fields_blocks_incomplete_config():
    issues = validate_provision_form_fields(
        profile_id=None,
        onu_mode="routing",
        mgmt_vlan_id=None,
        mgmt_ip_mode="static",
        mgmt_ip_address="172.16.1.10",
        mgmt_subnet=None,
        mgmt_gateway="bad-ip",
        wan_protocol="pppoe",
        wan_vlan_id=None,
        pppoe_username=None,
        static_ip_pool_id=None,
        static_ip=None,
        static_subnet=None,
        static_gateway=None,
        static_dns=None,
        lan_ip=None,
        lan_subnet="255.255.255.0",
        dhcp_enabled=True,
        dhcp_start=None,
        dhcp_end="192.168.1.254",
        wifi_enabled=True,
        wifi_ssid=None,
        wifi_password="short",
    )

    assert "Select service profile" in issues
    assert "Select management VLAN" in issues
    assert "Management subnet is required" in issues
    assert "Management gateway is invalid" in issues
    assert "Select internet VLAN" in issues
    assert "Enter PPPoE username" in issues
    assert "LAN gateway IP is required" in issues
    assert "DHCP start is required" in issues
    assert "Enter WiFi SSID" in issues
    assert "WiFi password must be at least 8 characters" in issues


def test_validate_provision_form_fields_allows_complete_dhcp_config():
    issues = validate_provision_form_fields(
        profile_id="profile-1",
        onu_mode="routing",
        mgmt_vlan_id="mgmt-vlan",
        mgmt_ip_mode="dhcp",
        mgmt_ip_address=None,
        mgmt_subnet=None,
        mgmt_gateway=None,
        wan_protocol="dhcp",
        wan_vlan_id="internet-vlan",
        pppoe_username=None,
        static_ip_pool_id=None,
        static_ip=None,
        static_subnet=None,
        static_gateway=None,
        static_dns=None,
        lan_ip="192.168.1.1",
        lan_subnet="255.255.255.0",
        dhcp_enabled=True,
        dhcp_start="192.168.1.100",
        dhcp_end="192.168.1.200",
        wifi_enabled=False,
        wifi_ssid=None,
        wifi_password=None,
    )

    assert issues == []
