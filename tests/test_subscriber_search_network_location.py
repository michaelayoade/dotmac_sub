from app.models.catalog import NasDevice, NasVendor
from app.models.network import FdhCabinet, OntAssignment, OntUnit, PonPort, Splitter
from app.models.subscriber import UserType
from app.services import subscriber as subscriber_service


def test_subscriber_search_matches_access_point(db_session, subscription):
    subscription.subscriber.user_type = UserType.customer
    nas = NasDevice(
        name="AP Terrace 7",
        code="AP-T7",
        vendor=NasVendor.mikrotik,
    )
    db_session.add(nas)
    db_session.flush()

    subscription.provisioning_nas_device_id = nas.id
    db_session.commit()

    items = subscriber_service.subscribers.list(
        db_session,
        organization_id=None,
        subscriber_type="person",
        order_by="created_at",
        order_dir="desc",
        limit=20,
        offset=0,
        search="AP Terrace 7",
    )

    assert any(item.id == subscription.subscriber_id for item in items)


def test_subscriber_search_matches_pop_site(db_session, subscription, pop_site):
    subscription.subscriber.user_type = UserType.customer
    nas = NasDevice(
        name="Distribution AP",
        code="DIST-AP",
        vendor=NasVendor.mikrotik,
        pop_site_id=pop_site.id,
    )
    db_session.add(nas)
    db_session.flush()

    subscription.provisioning_nas_device_id = nas.id
    db_session.commit()

    items = subscriber_service.subscribers.list(
        db_session,
        organization_id=None,
        subscriber_type="person",
        order_by="created_at",
        order_dir="desc",
        limit=20,
        offset=0,
        search=pop_site.name,
    )

    assert any(item.id == subscription.subscriber_id for item in items)


def test_subscriber_search_matches_cabinet(db_session, subscription, olt_device):
    subscription.subscriber.user_type = UserType.customer
    cabinet = FdhCabinet(name="Kabinet Alpha", code="FDH-A1")
    db_session.add(cabinet)
    db_session.flush()

    splitter = Splitter(name="Splitter A", fdh_id=cabinet.id)
    db_session.add(splitter)
    db_session.flush()

    ont = OntUnit(serial_number="ONT-CAB-001", splitter_id=splitter.id)
    db_session.add(ont)
    db_session.flush()

    pon_port = PonPort(olt_id=olt_device.id, name="PON 1")
    db_session.add(pon_port)
    db_session.flush()

    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    items = subscriber_service.subscribers.list(
        db_session,
        organization_id=None,
        subscriber_type="person",
        order_by="created_at",
        order_dir="desc",
        limit=20,
        offset=0,
        search=cabinet.name,
    )

    assert any(item.id == subscription.subscriber_id for item in items)
