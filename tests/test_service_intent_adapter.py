from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_translate_catalog_offer_prefers_radius_network_policy() -> None:
    from app.services.service_intent_adapter import translate_catalog_offer

    offer = SimpleNamespace(
        name="Fiber 100",
        code="FIBER100",
        service_type="internet",
        access_type="fiber",
        plan_category="internet",
        speed_download_mbps=100,
        speed_upload_mbps=50,
        guaranteed_speed_limit_at=80,
        priority="gold",
        burst_profile="burst-100",
        default_ont_profile_id=uuid4(),
    )
    profile_id = uuid4()
    radius_profile = SimpleNamespace(
        id=profile_id,
        name="GPON 200M",
        code="gpon-200",
        download_speed=200_000,
        upload_speed=100_000,
        vlan_id=203,
        inner_vlan_id=3003,
        ip_pool_name="pool-v4",
        ipv6_pool_name="pool-v6",
        mikrotik_rate_limit="200M/100M",
    )

    params = translate_catalog_offer(
        offer,
        radius_profile=radius_profile,
        provisioning_nas_device_id=uuid4(),
    )

    assert params.offer_code == "FIBER100"
    assert params.download_kbps == 200_000
    assert params.upload_kbps == 100_000
    assert params.qos_profile == "200M/100M"
    assert params.radius_profile_id == str(profile_id)
    assert params.s_vlan == 203
    assert params.c_vlan == 3003
    assert params.ip_pool_name == "pool-v4"
    assert params.default_ont_profile_id == str(offer.default_ont_profile_id)


def test_build_spec_from_subscription_returns_network_safe_payload() -> None:
    from app.services.service_intent_adapter import (
        build_network_payload_from_subscription,
        build_provisioning_spec_from_subscription,
    )

    subscriber_id = uuid4()
    subscription_id = uuid4()
    offer_id = uuid4()
    subscriber = SimpleNamespace(
        id=subscriber_id,
        account_number="CUST-1001",
        display_name="Ada Customer",
        email="ada@example.net",
        phone="+2348012345678",
    )
    offer = SimpleNamespace(
        id=offer_id,
        name="Fiber 50",
        code="FIBER50",
        service_type="internet",
        access_type="fiber",
        plan_category="internet",
        speed_download_mbps=50,
        speed_upload_mbps=20,
        guaranteed_speed_limit_at=None,
        priority=None,
        burst_profile=None,
        default_ont_profile_id=None,
    )
    subscription = SimpleNamespace(
        id=subscription_id,
        offer_id=offer_id,
        status="active",
        subscriber=subscriber,
        offer=offer,
        service_address=SimpleNamespace(
            id=uuid4(),
            street="1 GPON Way",
            city="Lagos",
            state="LA",
            postal_code="100001",
        ),
        service_address_id=None,
        provisioning_nas_device_id=None,
        radius_profile=None,
        radius_profile_id=None,
        login="CUST-1001@dotmac",
        ipv4_address="100.64.1.10",
        ipv6_address=None,
        mac_address="aa:bb:cc:dd:ee:ff",
        service_description="Primary internet",
    )

    spec = build_provisioning_spec_from_subscription(None, subscription)
    payload = build_network_payload_from_subscription(None, subscription)

    assert spec.subscription_id == str(subscription_id)
    assert spec.offer_id == str(offer_id)
    assert spec.subscriber.subscriber_id == str(subscriber_id)
    assert spec.subscriber.display_name == "Ada Customer"
    assert spec.network.download_kbps == 50_000
    assert spec.network.upload_kbps == 20_000
    assert spec.pppoe_login == "CUST-1001@dotmac"
    assert payload["subscriber"]["display_name"] == "Ada Customer"
    assert payload["network"]["offer_code"] == "FIBER50"
    assert "Subscriber(" not in repr(payload)


def test_build_olt_provisioning_spec_uses_scalar_intent_only() -> None:
    from app.services.service_intent_adapter import (
        ServiceIntentAdapter,
        SubscriberServiceRef,
        SubscriptionProvisioningSpec,
    )

    adapter = ServiceIntentAdapter()
    intent = SubscriptionProvisioningSpec(
        subscription_id="sub-1",
        offer_id="offer-1",
        status="active",
        subscriber=SubscriberServiceRef(
            subscriber_id="subscriber-1",
            display_name="Network Safe Name",
        ),
        network=adapter.translate_offer(
            SimpleNamespace(
                name="Fiber 20",
                code="FIBER20",
                service_type="internet",
                access_type="fiber",
                plan_category="internet",
                speed_download_mbps=20,
                speed_upload_mbps=10,
                guaranteed_speed_limit_at=None,
                priority=None,
                burst_profile=None,
                default_ont_profile_id=None,
            ),
            radius_profile=SimpleNamespace(
                id=uuid4(),
                name="VLAN 203",
                code="vlan203",
                download_speed=None,
                upload_speed=None,
                vlan_id=203,
                inner_vlan_id=None,
                ip_pool_name=None,
                ipv6_pool_name=None,
                mikrotik_rate_limit=None,
            ),
        ),
    )

    provisioning_spec = adapter.build_olt_provisioning_spec(intent, gem_index=2)

    assert len(provisioning_spec.wan_services) == 1
    wan_service = provisioning_spec.wan_services[0]
    assert wan_service.service_type == "internet"
    assert wan_service.vlan_id == 203
    assert wan_service.gem_index == 2
    assert wan_service.user_vlan == 203


def test_ui_adapter_builds_service_port_defaults_from_profile_intent() -> None:
    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    ont = SimpleNamespace()
    actual_ports = [SimpleNamespace(vlan_id=999)]

    defaults = service_intent_ui_adapter.profile_service_port_defaults(
        ont,
        service_ports=actual_ports,
    )

    assert defaults["primary_vlan_id"] is None
    assert defaults["primary_gem_index"] == 1
    assert defaults["primary_user_vlan"] is None
    assert defaults["primary_tag_transform"] == "default"
    assert defaults["missing_vlans"] == []
    assert defaults["extra_vlans"] == []
    assert defaults["planned_services"] == []


def test_acs_service_intent_adapter_maps_observed_summary_without_secrets() -> None:
    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    summary = SimpleNamespace(
        available=True,
        source="live",
        fetched_at=None,
        error=None,
        system={
            "Manufacturer": "Huawei",
            "Model": "HG8245H",
            "Firmware": "V5R020",
            "Hardware": "VER.A",
            "Serial": "HWTC12345678",
            "Uptime": "4d 2h 1m",
            "CPU Usage": "12",
            "Memory Total": "131072",
            "Memory Free": "65536",
            "Memory Usage": "50.0%",
            "MAC Address": "aa:bb:cc:dd:ee:ff",
        },
        wan={
            "Connection Type": "PPPoE",
            "WAN IP": "100.64.1.20",
            "Username": "cust-1001@dotmac",
            "Status": "Connected",
            "Uptime": "3600",
            "DNS Servers": "1.1.1.1,8.8.8.8",
            "Gateway": "100.64.1.1",
            "WAN Instance": "1.1",
            "WAN Service": "INTERNET",
        },
        lan={
            "LAN IP": "192.168.1.1",
            "Subnet Mask": "255.255.255.0",
            "DHCP Enabled": "1",
            "DHCP Start": "192.168.1.2",
            "DHCP End": "192.168.1.254",
            "Connected Hosts": "3",
        },
        wireless={
            "Enabled": "true",
            "SSID": "DOTMAC-1001",
            "Channel": "6",
            "Standard": "802.11n",
            "Security Mode": "WPA2-Personal",
            "Connected Clients": "2",
            "Password": "super-secret-password",
        },
        ethernet_ports=[
            {
                "index": 1,
                "Enable": "1",
                "Status": "Up",
                "MaxBitRate": "1000",
                "DuplexMode": "Full",
                "MACAddress": "11:22:33:44:55:66",
            }
        ],
        lan_hosts=[
            {
                "HostName": "phone",
                "IPAddress": "192.168.1.10",
                "MACAddress": "aa:bb:cc:00:11:22",
                "InterfaceType": "Ethernet",
                "Active": "1",
            }
        ],
    )

    intent = service_intent_ui_adapter.build_acs_observed_service_intent(summary)
    observed = intent["observed"]

    assert intent["available"] is True
    assert intent["source"] == "live"
    assert observed["system"]["manufacturer"] == "Huawei"
    assert observed["system"]["memory_usage"] == "50.0%"
    assert observed["wan"]["connection_type"] == "PPPoE"
    assert observed["wan"]["wan_ip"] == "100.64.1.20"
    assert observed["wan"]["pppoe_username"] == "cust-1001@dotmac"
    assert observed["lan"]["dhcp_enabled"] is True
    assert observed["wifi"]["enabled"] is True
    assert observed["wifi"]["ssid"] == "DOTMAC-1001"
    assert observed["wifi"]["password_present"] is True
    assert observed["ethernet_ports"][0]["port"] == 1
    assert observed["ethernet_ports"][0]["admin_enabled"] is True
    assert observed["ethernet_ports"][0]["link_status"] == "Up"
    assert observed["ethernet_ports"][0]["speed_mbps"] == "1000"
    assert observed["ethernet_ports"][0]["duplex"] == "Full"
    assert observed["ethernet_ports"][0]["mac_address"] == "11:22:33:44:55:66"
    assert observed["lan_hosts"][0]["host_name"] == "phone"
    assert observed["lan_hosts"][0]["ip_address"] == "192.168.1.10"
    assert observed["lan_hosts"][0]["active"] is True
    assert observed["lan_hosts"][0]["active_display"] == "Active"
    tracked = intent["tracked_point_index"]
    assert tracked["system.hardware"]["raw_value"] == "VER.A"
    assert tracked["system.cpu_usage"]["raw_value"] == "12"
    assert tracked["wan.wan_instance"]["raw_value"] == "1.1"
    assert tracked["wan.wan_service"]["raw_value"] == "INTERNET"
    assert tracked["clients.ethernet_ports_total"]["raw_value"] == 1
    assert tracked["clients.lan_hosts_active"]["raw_value"] == 1
    assert "super-secret-password" not in repr(intent)


def test_acs_service_intent_adapter_hides_metadata_only_wan_nodes() -> None:
    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    metadata_node = {"_object": False, "_writable": False}
    intent = service_intent_ui_adapter.build_acs_observed_service_intent(
        SimpleNamespace(
            available=True,
            source="cache",
            fetched_at=None,
            error=None,
            system={},
            wan={
                "Connection Type": "IP_Routed",
                "WAN IP": metadata_node,
                "Username": "100025520",
                "Status": metadata_node,
                "Uptime": metadata_node,
                "DNS Servers": {"_object": False, "_writable": True},
                "Gateway": metadata_node,
            },
            lan={},
            wireless={},
            ethernet_ports=[],
            lan_hosts=[],
        )
    )

    wan = intent["observed"]["wan"]
    assert wan["connection_type"] == "IP_Routed"
    assert wan["pppoe_username"] == "100025520"
    assert wan["wan_ip"] is None
    assert wan["status"] is None
    assert wan["gateway"] is None
    assert wan["dns_servers"] is None
    assert "{'_object'" not in repr(intent)


def test_acs_service_intent_adapter_preserves_tracked_points_for_partial_unavailable_data() -> None:
    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    intent = service_intent_ui_adapter.build_acs_observed_service_intent(
        SimpleNamespace(
            available=False,
            source="cache",
            fetched_at=None,
            error="Connection request failed.",
            system={"Manufacturer": "Huawei"},
            wan={"WAN IP": "100.64.1.20", "Status": "Connected"},
            lan={},
            wireless={},
            ethernet_ports=[],
            lan_hosts=[],
        )
    )

    assert intent["available"] is False
    assert intent["sections"]
    assert intent["tracked_point_index"]["system.manufacturer"]["raw_value"] == "Huawei"
    assert intent["tracked_point_index"]["wan.wan_ip"]["raw_value"] == "100.64.1.20"


def test_acs_service_intent_adapter_counts_active_ethernet_ports_by_link_status() -> None:
    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    intent = service_intent_ui_adapter.build_acs_observed_service_intent(
        SimpleNamespace(
            available=True,
            source="live",
            fetched_at=None,
            error=None,
            system={},
            wan={},
            lan={},
            wireless={},
            ethernet_ports=[
                {"index": 1, "Enable": "1", "Status": "Up"},
                {"index": 2, "Enable": "1", "Status": "Down"},
            ],
            lan_hosts=[],
        )
    )

    assert intent["tracked_point_index"]["clients.ethernet_ports_total"]["raw_value"] == 2
    assert intent["tracked_point_index"]["clients.ethernet_ports_active"]["raw_value"] == 1


def test_cached_tr069_snapshot_hides_metadata_only_parameter_nodes() -> None:
    from app.services.network.ont_tr069 import OntTR069

    summary = OntTR069._summary_from_snapshot(
        SimpleNamespace(
            id="ont-1",
            tr069_last_snapshot={
                "wan": {
                    "Connection Type": "IP_Routed",
                    "WAN IP": {"_object": False, "_writable": False},
                    "Username": {"_value": "100025520"},
                    "Status": {"_object": False, "_writable": False},
                    "DNS Servers": {"_object": False, "_writable": True},
                },
                "system": {},
                "lan": {},
                "wireless": {},
                "ethernet_ports": [],
                "lan_hosts": [],
            },
            tr069_last_snapshot_at=None,
            observed_runtime_updated_at=None,
        )
    )

    assert summary is not None
    assert summary.wan["Connection Type"] == "IP_Routed"
    assert summary.wan["Username"] == "100025520"
    assert summary.wan["WAN IP"] is None
    assert summary.wan["Status"] is None
    assert summary.wan["DNS Servers"] is None


def test_service_intent_ui_adapter_delegates_ont_capabilities(monkeypatch) -> None:
    from app.services.network.ont_read import OntReadFacade
    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    calls = {}

    def fake_capabilities(db, ont_id):
        calls["args"] = (db, ont_id)
        return {"supports_wifi": True}

    monkeypatch.setattr(OntReadFacade, "get_capabilities", fake_capabilities)

    db = object()
    result = service_intent_ui_adapter.ont_capabilities(db, ont_id="ont-1")

    assert result == {"supports_wifi": True}
    assert calls["args"] == (db, "ont-1")


def test_acs_service_intent_adapter_handles_unavailable_summary() -> None:
    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    intent = service_intent_ui_adapter.build_acs_observed_service_intent(
        SimpleNamespace(
            available=False,
            source="none",
            fetched_at=None,
            error="No matching CPE device.",
            system={},
            wan={},
            lan={},
            wireless={},
            ethernet_ports=[],
            lan_hosts=[],
        )
    )

    assert intent["available"] is False
    assert intent["sections"] == []
    assert intent["error"] == "No matching CPE device."
