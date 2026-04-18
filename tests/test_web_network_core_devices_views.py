from datetime import UTC, datetime

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import (
    OLTDevice,
    OntAcsStatus,
    OntAssignment,
    OntStatusSource,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
)
from app.models.provisioning import (
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningWorkflow,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import web_network_core_devices_views as core_devices_views


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

    ont = OntUnit(serial_number="ONT-DETAIL-NAME-001", is_active=True, olt_device_id=olt.id)
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


def test_ont_detail_page_data_exposes_connected_customer_device_count_from_snapshot(
    db_session,
):
    ont = OntUnit(
        serial_number="ONT-LAN-HOSTS",
        is_active=True,
        observed_lan_hosts=9,
        observed_wifi_clients=8,
        tr069_last_snapshot={
            "wireless": {"Connected Clients": "2"},
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
            ]
        },
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.ont_detail_page_data(db_session, str(ont.id))

    assert payload is not None
    assert payload["connected_customer_devices"] == 2
    assert payload["connected_wifi_clients"] == 2


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
    assert payload["connected_wifi_clients"] == 3


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
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.onts_list_page_data(db_session, per_page=50)
    signal = payload["signal_data"][str(ont.id)]

    assert signal["status_display"] == ""
    assert signal["olt_status"] is None
    assert signal["olt_status_display"] == ""


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
                    {"step_type": "create_olt_service_port", "status": "success", "detail": "Service port created"},
                    {"step_type": "push_tr069_pppoe_credentials", "status": "success", "detail": "PPPoE pushed"},
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
