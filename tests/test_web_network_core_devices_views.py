from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import web_network_core_devices_views as core_devices_views


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
            subscription_id=subscription.id,
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
