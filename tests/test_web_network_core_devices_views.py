from datetime import UTC, datetime
from pathlib import Path

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.compensation_failure import CompensationFailure, CompensationStatus
from app.models.network import (
    ConfigMethod,
    IpProtocol,
    MgmtIpMode,
    OLTDevice,
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntConfigOverride,
    OntAcsStatus,
    OntAssignment,
    OntAuthorizationStatus,
    OntProfileWanService,
    OntProvisioningProfile,
    OntStatusSource,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    WanConnectionType,
    WanMode,
)
from app.models.provisioning import (
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningWorkflow,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import web_network_core_devices_views as core_devices_views
from tests.legacy_ont_profile_link import seed_legacy_profile_link


def test_display_ont_serial_decodes_huawei_hex() -> None:
    assert core_devices_views._display_ont_serial("4857544308D90492") == "HWTC08D90492"
    assert core_devices_views._display_ont_serial("HWTC03217F84") == "HWTC03217F84"


def test_display_ont_serial_hides_generated_placeholders() -> None:
    assert (
        core_devices_views._display_ont_serial("HW-86BF78E7-04104-2604111358482263")
        == ""
    )
    assert core_devices_views._display_ont_serial("ZT-86BF78E7-04104") == ""


def test_ont_detail_page_data_uses_unified_subscriber_name_and_status(db_session):
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Lovelace",
        email="ada.lovelace@example.com",
        display_name="Ada Lovelace",
        status=SubscriberStatus.active,
    )
    db_session.add(subscriber)
    db_session.flush()

    offer = CatalogOffer(
        name="ONT Detail Plan",
        status=OfferStatus.active,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db_session.add(offer)
    db_session.flush()

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)

    olt = OLTDevice(name="OLT-ONT-DETAIL", mgmt_ip="198.51.100.210")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(olt_id=olt.id, name="0/1/0", is_active=True)
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-DETAIL-NAME-001", is_active=True, olt_device_id=olt.id
    )
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["subscriber_info"]["id"] == str(subscriber.id)
    assert payload["subscriber_info"]["name"] == "Ada Lovelace"
    assert payload["subscriber_info"]["status"] == "active"
    assert "emerald" in payload["subscriber_info"]["status_class"]
    assert payload["subscriber_info"]["subscription_status"] == "active"


def test_ont_detail_page_data_exposes_display_serial_number(db_session):
    olt = OLTDevice(name="OLT-HEX-SERIAL", mgmt_ip="198.51.100.212")
    db_session.add(olt)
    db_session.flush()

    ont = OntUnit(
        serial_number="4857544308D90492",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["display_serial_number"] == "HWTC08D90492"
    assert payload["display_serial_label"] == "HWTC08D90492"
    assert payload["identity_label"] == "Huawei"


def test_ont_detail_page_data_infers_identity_from_huawei_serial(db_session):
    ont = OntUnit(
        serial_number="4857544328201B9A",
        model="HG8546M",
        is_active=True,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["display_serial_label"] == "HWTC28201B9A"
    assert payload["identity_label"] == "Huawei HG8546M"


def test_ont_detail_page_data_hides_synthetic_serial(db_session):
    olt = OLTDevice(name="OLT-SYNTH-SERIAL", mgmt_ip="198.51.100.214")
    db_session.add(olt)
    db_session.flush()

    ont = OntUnit(
        serial_number="HW-86BF78E7-04104-2604111358482263",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["display_serial_number"] == ""
    assert payload["display_serial_label"] == "-"


def test_ont_detail_page_data_profile_state_uses_active_bundle_assignment(
    db_session, monkeypatch
):
    legacy_profile = OntProvisioningProfile(name="Legacy Detail Profile", is_active=True)
    assigned_bundle = OntProvisioningProfile(name="Assigned Detail Bundle", is_active=True)
    db_session.add_all([legacy_profile, assigned_bundle])
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-DETAIL-BUNDLE-STATE-001",
        is_active=True,
        provisioning_status=None,
    )
    seed_legacy_profile_link(ont, legacy_profile)
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=assigned_bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_profile_apply.detect_drift",
        lambda _db, _ont_id: None,
    )

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["profile_state"]["profile_id"] == str(assigned_bundle.id)
    assert payload["profile_state"]["profile_name"] == "Assigned Detail Bundle"


def test_ont_detail_page_data_sanitizes_impossible_optical_values(db_session):
    ont = OntUnit(
        serial_number="ONT-BAD-OPTICAL-001",
        is_active=True,
        olt_rx_signal_dbm=21474836.47,
        onu_rx_signal_dbm=-21.5,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["signal_info"]["olt_rx_dbm"] is None
    assert payload["signal_info"]["olt_quality"] == "unknown"
    assert payload["signal_info"]["onu_rx_dbm"] == -21.5


def test_ont_detail_page_data_exposes_connected_customer_device_count_from_snapshot(
    db_session,
):
    ont = OntUnit(
        serial_number="ONT-LAN-HOSTS",
        is_active=True,
        observed_lan_hosts=9,
        observed_wifi_clients=8,
        tr069_last_snapshot={
            "wan": {
                "WAN IP": "100.64.20.10",
                "Status": "Connected",
                "Username": "ada@example",
            },
            "lan": {"LAN IP": "192.168.88.1", "DHCP Enabled": "true"},
            "wireless": {"Connected Clients": "2"},
            "ethernet_ports": [
                {"Status": "Up"},
                {"Status": "Down"},
            ],
            "lan_hosts": [
                {
                    "HostName": "phone",
                    "IPAddress": "192.168.1.10",
                    "MACAddress": "AA:BB:CC:00:00:01",
                    "Active": "true",
                },
                {
                    "HostName": "laptop",
                    "IPAddress": "192.168.1.11",
                    "MACAddress": "AA:BB:CC:00:00:02",
                    "Active": "1",
                },
                {
                    "HostName": "old-device",
                    "IPAddress": "192.168.1.12",
                    "MACAddress": "AA:BB:CC:00:00:03",
                    "Active": "false",
                },
            ],
        },
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["connected_customer_devices"] == 2
    assert payload["connected_wifi_clients"] == 2
    assert payload["last_config_summary"]["wan_ip"] == "100.64.20.10"
    assert payload["last_config_summary"]["wan_status"] == "Connected"
    assert payload["last_config_summary"]["pppoe_user"] == "ada@example"
    assert payload["last_config_summary"]["lan_ip"] == "192.168.88.1"
    assert payload["last_config_summary"]["dhcp_enabled"] == "Enabled"
    assert payload["last_config_summary"]["active_ports"] == 1
    assert payload["last_config_summary"]["ethernet_ports"] == 2


def test_ont_detail_page_data_falls_back_to_observed_lan_hosts(db_session):
    ont = OntUnit(
        serial_number="ONT-LAN-HOSTS-FALLBACK",
        is_active=True,
        observed_lan_hosts=4,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["connected_customer_devices"] == 4


def test_ont_detail_page_data_uses_wifi_clients_when_host_count_missing(db_session):
    ont = OntUnit(
        serial_number="ONT-WIFI-CLIENTS-FALLBACK",
        is_active=True,
        observed_wifi_clients=3,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["connected_customer_devices"] == 3


def test_ont_detail_page_data_uses_effective_bundle_values_in_summaries(db_session):
    olt = OLTDevice(name="OLT-DETAIL-EFFECTIVE", mgmt_ip="198.51.100.215")
    db_session.add(olt)
    db_session.flush()

    bundle = OntProvisioningProfile(
        name="Detail Effective Bundle",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_vlan_tag=911,
        wifi_enabled=True,
        wifi_ssid_template="bundle-ssid",
        wifi_channel="6",
        config_method=ConfigMethod.tr069,
        ip_protocol=IpProtocol.dual_stack,
        is_active=True,
    )
    db_session.add(bundle)
    db_session.flush()

    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=912,
            connection_type=WanConnectionType.pppoe,
            is_active=True,
        )
    )

    ont = OntUnit(
        serial_number="DETAIL-EFFECTIVE-ONT-001",
        is_active=True,
        olt_device_id=olt.id,
        wan_mode=WanMode.static_ip,
        pppoe_username="legacy-user",
        mgmt_ip_mode=MgmtIpMode.dhcp,
        tr069_last_snapshot={},
    )
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.add_all(
        [
            OntConfigOverride(
                ont_unit_id=ont.id,
                field_name="wan.pppoe_username",
                value_json={"value": "override-user"},
            ),
            OntConfigOverride(
                ont_unit_id=ont.id,
                field_name="management.ip_address",
                value_json={"value": "10.91.0.2"},
            ),
        ]
    )
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["observed_runtime_summary"]["pppoe_user"] == "override-user"
    assert payload["observed_runtime_summary"]["wan_mode"] == "pppoe"

    rows = {row["label"]: row["value"] for row in payload["desired_config_summary"]["rows"]}
    assert rows["Mgmt mode"] == "static_ip"
    assert rows["Mgmt VLAN"] == "911"
    assert rows["Mgmt IP"] == "10.91.0.2"
    assert rows["WAN mode"] == "pppoe"
    assert rows["IP protocol"] == "Dual Stack (IPv4 + IPv6)"
    assert rows["WAN VLAN"] == "912"
    assert rows["PPPoE user"] == "override-user"
    assert rows["SSID"] == "bundle-ssid"
    assert rows["WiFi channel"] == "6"


def test_ont_detail_page_data_uses_recent_acs_inform_for_effective_online_status(
    db_session,
):
    olt = OLTDevice(name="OLT-ACS-STATUS", mgmt_ip="198.51.100.213")
    db_session.add(olt)
    db_session.flush()

    acs = Tr069AcsServer(name="ACS", base_url="http://genieacs.local")
    db_session.add(acs)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-ACS-1",
        is_active=True,
        olt_device_id=olt.id,
        online_status=OnuOnlineStatus.offline,
    )
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        Tr069CpeDevice(
            acs_server_id=acs.id,
            ont_unit_id=ont.id,
            serial_number=ont.serial_number,
            genieacs_device_id="device-1",
            last_inform_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["signal_info"]["online_status"] == "online"
    assert payload["signal_info"]["online_status_source"] == "acs"
    assert payload["signal_info"]["olt_status"] == "offline"
    assert payload["signal_info"]["acs_status"] == "online"
    assert payload["signal_info"]["last_seen_at"] == payload["signal_info"]["acs_last_inform_at"]


def test_ont_detail_page_data_blanks_unknown_online_status(db_session):
    ont = OntUnit(
        serial_number="ONT-UNKNOWN-BLANK",
        is_active=True,
        online_status=OnuOnlineStatus.unknown,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["signal_info"]["online_status"] is None
    assert payload["signal_info"]["online_status_display"] == ""
    assert payload["signal_info"]["olt_status"] is None
    assert payload["signal_info"]["olt_status_display"] == ""


def test_onts_list_page_data_online_filter_includes_recent_acs_devices(db_session):
    olt = OLTDevice(name="OLT-ACS-FILTER", mgmt_ip="198.51.100.214")
    db_session.add(olt)
    db_session.flush()

    acs = Tr069AcsServer(name="ACS", base_url="http://genieacs.local")
    db_session.add(acs)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-ACS-FILTER",
        is_active=True,
        olt_device_id=olt.id,
        online_status=OnuOnlineStatus.offline,
        acs_status=OntAcsStatus.online,
        acs_last_inform_at=datetime.now(UTC),
        effective_status=OnuOnlineStatus.online,
        effective_status_source=OntStatusSource.acs,
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        Tr069CpeDevice(
            acs_server_id=acs.id,
            ont_unit_id=ont.id,
            serial_number=ont.serial_number,
            genieacs_device_id="device-2",
            last_inform_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.onts_list_page_data(
        db_session,
        olt_id=str(olt.id),
        online_status="online",
        per_page=50,
    )

    assert [item.serial_number for item in payload["onts"]] == ["ONT-ACS-FILTER"]
    assert payload["signal_data"][str(ont.id)]["status_display"] == "Online"
    assert payload["signal_data"][str(ont.id)]["status_source"] == "acs"
    assert payload["signal_data"][str(ont.id)]["olt_status"] == "offline"
    assert payload["signal_data"][str(ont.id)]["acs_status"] == "online"


def test_onts_list_page_data_blanks_unknown_online_status(db_session):
    ont = OntUnit(
        serial_number="ONT-LIST-UNKNOWN-BLANK",
        is_active=True,
        online_status=OnuOnlineStatus.unknown,
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.onts_list_page_data(db_session, per_page=50)
    signal = payload["signal_data"][str(ont.id)]

    assert signal["status_display"] == ""
    assert signal["olt_status"] is None
    assert signal["olt_status_display"] == ""


def test_onts_list_page_data_defaults_to_authorized_inventory(db_session):
    authorized = OntUnit(
        serial_number="ONT-AUTHORIZED-LIST",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
    )
    pending = OntUnit(
        serial_number="ONT-PENDING-LIST",
        is_active=True,
        authorization_status=OntAuthorizationStatus.pending,
    )
    unknown = OntUnit(
        serial_number="ONT-UNKNOWN-AUTH-LIST",
        is_active=True,
        authorization_status=None,
    )
    db_session.add_all([authorized, pending, unknown])
    db_session.commit()

    default_payload = core_devices_views.onts_list_page_data(db_session, per_page=50)
    unauthorized_payload = core_devices_views.onts_list_page_data(
        db_session,
        authorization="unauthorized",
        per_page=50,
    )
    all_payload = core_devices_views.onts_list_page_data(
        db_session,
        authorization="all",
        per_page=50,
    )

    assert [ont.serial_number for ont in default_payload["onts"]] == [
        "ONT-AUTHORIZED-LIST"
    ]
    assert [ont.serial_number for ont in unauthorized_payload["onts"]] == [
        "ONT-PENDING-LIST",
        "ONT-UNKNOWN-AUTH-LIST",
    ]
    assert [ont.serial_number for ont in all_payload["onts"]] == [
        "ONT-AUTHORIZED-LIST",
        "ONT-PENDING-LIST",
        "ONT-UNKNOWN-AUTH-LIST",
    ]
    assert default_payload["filters"]["authorization"] == "authorized"
    assert unauthorized_payload["filters"]["authorization"] == "unauthorized"
    assert all_payload["filters"]["authorization"] == "all"


def test_onts_list_page_data_uses_direct_olt_pon_topology_without_assignment(
    db_session,
):
    olt = OLTDevice(name="OLT-DIRECT-TABLE", mgmt_ip="198.51.100.222")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(
        olt_id=olt.id,
        name="0/2/9",
        port_number=9,
        notes="Feeder 9",
        is_active=True,
    )
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-DIRECT-PON",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="9",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.onts_list_page_data(db_session, per_page=50)

    info = payload["assignment_info"][str(ont.id)]
    assert info["olt_name"] == "OLT-DIRECT-TABLE"
    assert info["olt_id"] == str(olt.id)
    assert info["pon_port_name"] == "0/2/9"
    assert info["pon_port_display"] == "9 - Feeder 9"
    assert info["subscriber_name"] == ""


def test_onts_list_page_data_merges_normalized_serial_assignment_and_direct_topology(
    db_session,
):
    subscriber = Subscriber(
        first_name="TechSquad",
        last_name="Africa Asokoro",
        email="techsquad.asokoro@example.com",
        display_name="TechSquad Africa Asokoro",
        status=SubscriberStatus.active,
    )
    olt = OLTDevice(name="BOI Huawei OLT", mgmt_ip="198.51.100.224")
    db_session.add_all([subscriber, olt])
    db_session.flush()

    pon = PonPort(
        olt_id=olt.id,
        name="0/1/6",
        port_number=6,
        is_active=True,
    )
    db_session.add(pon)
    db_session.flush()

    hex_ont = OntUnit(
        serial_number="4857544328201B9A",
        is_active=True,
        olt_device_id=olt.id,
        board="0/1",
        port="6",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    display_ont = OntUnit(
        serial_number="HWTC28201B9A",
        is_active=True,
        olt_device_id=olt.id,
        board="0/1",
        port="6",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add_all([hex_ont, display_ont])
    db_session.flush()

    db_session.add(
        OntAssignment(
            ont_unit_id=hex_ont.id,
            subscriber_id=subscriber.id,
            pon_port_id=None,
            active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.onts_list_page_data(
        db_session,
        search="HWTC28201B9A",
        per_page=50,
    )

    info = payload["assignment_info"][str(display_ont.id)]
    assert info["subscriber_name"] == "TechSquad Africa Asokoro"
    assert info["olt_name"] == "BOI Huawei OLT"
    assert info["pon_port_name"] == "0/1/6"
    assert info["pon_port_display"] == "6"


def test_onts_list_page_data_filters_by_direct_pon_topology_without_assignment(
    db_session,
):
    olt = OLTDevice(name="OLT-DIRECT-FILTER", mgmt_ip="198.51.100.223")
    db_session.add(olt)
    db_session.flush()

    pon_a = PonPort(olt_id=olt.id, name="0/3/1", is_active=True)
    pon_b = PonPort(olt_id=olt.id, name="0/3/2", is_active=True)
    db_session.add_all([pon_a, pon_b])
    db_session.flush()

    ont_a = OntUnit(
        serial_number="ONT-DIRECT-A",
        is_active=True,
        olt_device_id=olt.id,
        board="0/3",
        port="1",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    ont_b = OntUnit(
        serial_number="ONT-DIRECT-B",
        is_active=True,
        olt_device_id=olt.id,
        board="0/3",
        port="2",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add_all([ont_a, ont_b])
    db_session.commit()

    payload = core_devices_views.onts_list_page_data(
        db_session,
        pon_port_id=str(pon_a.id),
        per_page=50,
    )

    assert [ont.serial_number for ont in payload["onts"]] == ["ONT-DIRECT-A"]
    assert payload["filters"]["pon_port_id"] == str(pon_a.id)


def test_onts_list_page_data_searches_direct_olt_pon_topology_without_assignment(
    db_session,
):
    olt = OLTDevice(name="OLT-DIRECT-SEARCH", hostname="direct-search-olt")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(
        olt_id=olt.id,
        name="0/4/8",
        notes="Direct Search PON",
        is_active=True,
    )
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-DIRECT-SEARCH",
        is_active=True,
        olt_device_id=olt.id,
        board="0/4",
        port="8",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add(ont)
    db_session.commit()

    by_olt = core_devices_views.onts_list_page_data(
        db_session,
        search="direct-search-olt",
        per_page=50,
    )
    by_pon = core_devices_views.onts_list_page_data(
        db_session,
        search="0/4/8",
        per_page=50,
    )
    by_pon_notes = core_devices_views.onts_list_page_data(
        db_session,
        search="Direct Search PON",
        per_page=50,
    )

    assert [item.serial_number for item in by_olt["onts"]] == ["ONT-DIRECT-SEARCH"]
    assert [item.serial_number for item in by_pon["onts"]] == ["ONT-DIRECT-SEARCH"]
    assert [item.serial_number for item in by_pon_notes["onts"]] == [
        "ONT-DIRECT-SEARCH"
    ]


def test_ont_index_view_toggles_are_navigation_links() -> None:
    template = Path("templates/admin/network/onts/index.html").read_text()

    assert 'id="ont-fleet-page"' in template
    assert 'href="/admin/network/onts?view=list"' in template
    assert (
        'href="/admin/network/onts?view=diagnostics&order_by=signal&order_dir=asc"'
        in template
    )
    assert 'href="/admin/network/onts?view=unconfigured"' in template
    assert 'id="ont-filter-form" autocomplete="off"' in template
    assert 'autocomplete="off" autocapitalize="off" autocorrect="off"' in template
    assert 'hx-target="#ont-fleet-page"' in template
    assert 'hx-select="#ont-fleet-page"' in template
    assert 'hx-swap="outerHTML"' in template


def test_ont_detail_uses_all_config_operator_view() -> None:
    template = Path("templates/admin/network/onts/detail.html").read_text()
    unified_template = Path("templates/admin/network/onts/_unified_config.html").read_text()
    wan_template = Path("templates/admin/network/onts/_config_wan.html").read_text()
    profile_form_template = Path(
        "templates/admin/network/provisioning-profiles/form.html"
    ).read_text()
    profile_preview_template = Path(
        "templates/admin/network/onts/_profile_preview.html"
    ).read_text()

    assert "default('summary')" in template
    assert "Effective Config" in template
    assert "Observed State" in template
    assert "<span>Summary</span>" in template
    assert "<span>Effective Config</span>" in template
    assert "<span>Observed State</span>" in template
    assert "<span>History</span>" in template
    assert "<span>Diagnostics</span>" not in template
    assert "/unified-config" not in template
    assert '{% include "admin/network/onts/_unified_config.html" %}' in template
    assert "Loading device configuration..." not in template
    assert "OLT / Service Path" in unified_template
    assert "Observed Service Ports" in unified_template
    assert "Dual-stack IPv4/IPv6 with DHCPv6-PD intent" in unified_template
    assert "IPv6 PD" in unified_template
    assert "IPv4 + IPv6 using DHCPv6 prefix delegation" in wan_template
    assert "Dual Stack (IPv4 + IPv6 DHCPv6-PD)" in profile_form_template
    assert "Dual Stack (IPv4 + IPv6 DHCPv6-PD)" in profile_preview_template
    assert '_config_wan.html' in unified_template
    assert '_config_tr069_profile.html' in unified_template
    assert '_config_wifi.html' in unified_template
    assert '_config_lan.html' in unified_template
    assert "_service_ports_embedded.html" in unified_template
    assert "Loading WAN configuration..." not in unified_template
    assert "Loading TR-069 profiles..." not in unified_template


def test_ont_detail_page_data_includes_recent_provisioning_runs(db_session):
    subscriber = Subscriber(
        first_name="Grace",
        last_name="Hopper",
        email="grace.hopper@example.com",
        status=SubscriberStatus.active,
    )
    db_session.add(subscriber)
    db_session.flush()

    offer = CatalogOffer(
        name="Provisioned Fiber",
        status=OfferStatus.active,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db_session.add(offer)
    db_session.flush()

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)

    olt = OLTDevice(name="OLT-PROV", mgmt_ip="198.51.100.211")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(serial_number="ONT-PROV-001", is_active=True, olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    workflow = ProvisioningWorkflow(name="13-Step OLT Flow")
    db_session.add(workflow)
    db_session.flush()
    db_session.add(
        ProvisioningRun(
            workflow_id=workflow.id,
            subscription_id=subscription.id,
            status=ProvisioningRunStatus.success,
            output_payload={
                "results": [
                    {
                        "step_type": "create_olt_service_port",
                        "status": "success",
                        "detail": "Service port created",
                    },
                    {
                        "step_type": "push_tr069_pppoe_credentials",
                        "status": "success",
                        "detail": "PPPoE pushed",
                    },
                ]
            },
        )
    )
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["provisioning_runs"][0]["workflow_name"] == "13-Step OLT Flow"
    assert payload["provisioning_runs"][0]["step_count"] == 2
    assert payload["provisioning_runs"][0]["success_count"] == 2


def test_ont_detail_page_data_includes_compensation_failures(db_session):
    olt = OLTDevice(name="OLT-COMP", mgmt_ip="198.51.100.212")
    db_session.add(olt)
    db_session.flush()

    ont = OntUnit(serial_number="ONT-COMP-001", is_active=True, olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()

    failure = CompensationFailure(
        ont_unit_id=ont.id,
        olt_device_id=olt.id,
        operation_type="provisioning",
        step_name="create_service_port",
        undo_commands=["undo service-port 100"],
        description="Remove partially created service-port",
        error_message="timeout waiting for SSH response",
        failure_count=2,
        status=CompensationStatus.pending,
    )
    db_session.add(failure)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["compensation_failures"][0]["step_name"] == "create_service_port"
    assert payload["compensation_failures"][0]["status"] == "pending"
    assert payload["compensation_failures"][0]["failure_count"] == 2
    assert payload["compensation_failures"][0]["next_retry_at"] is not None
