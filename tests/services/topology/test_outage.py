"""Manual outage declare/resolve/list (Phase 4b, P4.4)."""

from __future__ import annotations

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import FdhCabinet, Splitter, SplitterPort, SplitterPortAssignment
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Subscriber
from app.services.topology.outage import (
    declare_outage,
    list_open_incidents,
    resolve_outage,
)


def _bts_with_subs(db, offer_id, n):
    pop = PopSite(name="Garki", zabbix_group_id="10")
    nas = NasDevice(name="NAS", management_ip="10.0.0.1")
    db.add_all([pop, nas])
    db.flush()
    db.add(
        NetworkDevice(
            name="node",
            matched_device_type="nas",
            matched_device_id=nas.id,
            pop_site_id=pop.id,
            is_active=True,
        )
    )
    for _ in range(n):
        s = Subscriber(first_name="A", last_name="B", email=f"{_}-{pop.id}@ex.com")
        db.add(s)
        db.flush()
        db.add(
            Subscription(
                subscriber_id=s.id,
                offer_id=offer_id,
                status=SubscriptionStatus.active,
                provisioning_nas_device_id=nas.id,
            )
        )
    db.flush()
    return pop


def test_declare_snapshots_affected_count(db_session, catalog_offer):
    pop = _bts_with_subs(db_session, catalog_offer.id, 3)
    inc = declare_outage(
        db_session, basestation=pop, declared_by="noc@x", note="fiber cut"
    )
    assert inc.status == "open"
    assert inc.affected_count == 3
    assert inc.basestation_id == pop.id
    assert inc.declared_by == "noc@x"


def test_declare_fdh_outage_snapshots_affected_count(db_session, catalog_offer):
    fdh = FdhCabinet(name="FDH Alpha", code="FDH-A")
    splitter = Splitter(name="Splitter Alpha", fdh=fdh)
    db_session.add_all([fdh, splitter])
    db_session.flush()
    ports = [
        SplitterPort(splitter_id=splitter.id, port_number=1),
        SplitterPort(splitter_id=splitter.id, port_number=2),
    ]
    db_session.add_all(ports)
    db_session.flush()
    for index, port in enumerate(ports):
        subscriber = Subscriber(
            first_name="F",
            last_name="DH",
            email=f"fdh-{index}@ex.com",
        )
        db_session.add(subscriber)
        db_session.flush()
        db_session.add(
            Subscription(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
            )
        )
        db_session.add(
            SplitterPortAssignment(
                splitter_port_id=port.id,
                subscriber_id=subscriber.id,
                active=True,
            )
        )
    db_session.flush()

    inc = declare_outage(db_session, fdh=fdh, declared_by="noc@x")
    assert inc.status == "open"
    assert inc.affected_count == 2
    assert inc.fdh_cabinet_id == fdh.id
    assert inc.declared_by == "noc@x"


def test_resolve_outage(db_session, catalog_offer):
    pop = _bts_with_subs(db_session, catalog_offer.id, 1)
    inc = declare_outage(db_session, basestation=pop)
    resolved = resolve_outage(db_session, inc.id)
    assert resolved.status == "resolved"
    assert resolved.resolved_at is not None


def test_list_open_excludes_resolved(db_session, catalog_offer):
    pop = _bts_with_subs(db_session, catalog_offer.id, 1)
    a = declare_outage(db_session, basestation=pop)
    b = declare_outage(db_session, basestation=pop)
    resolve_outage(db_session, a.id)
    open_ids = {i.id for i in list_open_incidents(db_session)}
    assert b.id in open_ids and a.id not in open_ids


def test_declare_requires_target(db_session):
    import pytest

    with pytest.raises(ValueError):
        declare_outage(db_session)
