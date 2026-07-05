"""Topology-gaps report + match-rate (Phase 1, Task 8)."""

from __future__ import annotations

import uuid

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Subscriber
from app.services.topology.gaps import topology_gaps


def _sub(subscriber_id, offer_id):
    return Subscription(
        subscriber_id=subscriber_id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
    )


def test_topology_gaps_counts_and_match_rate(db_session, subscriber, catalog_offer):
    # --- Complete-path subscriber (fiber): ONT -> OLT -> node -> pop_site ---
    olt = OLTDevice(name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1")
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(
        NetworkDevice(
            name="olt1-node",
            source="zabbix_reconcile",
            matched_device_type="olt",
            matched_device_id=olt.id,
            pop_site_id=pop.id,
            zabbix_hostid="201",
        )
    )
    ont = OntUnit(serial_number="SN-1", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    complete_sub = _sub(subscriber.id, catalog_offer.id)

    # --- Gappy subscriber: no ONT, no NAS ---
    other = Subscriber(
        first_name="No", last_name="Path", email=f"nopath-{uuid.uuid4().hex}@ex.com"
    )
    db_session.add(other)
    db_session.flush()
    gappy_sub = _sub(other.id, catalog_offer.id)

    # --- An unmatched Zabbix node (reconciled, no provisioning match) ---
    ghost = NetworkDevice(
        name="ghost-host",
        source="zabbix_reconcile",
        matched_device_id=None,
        mgmt_ip="192.0.2.9",
        zabbix_hostid="999",
        is_active=True,
    )
    db_session.add_all([complete_sub, gappy_sub, ghost])
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.unmatched_node_count == 1
    assert gaps.unmatched_nodes[0].name == "ghost-host"
    assert gaps.active_subscriptions == 2
    assert gaps.resolved_complete == 1
    assert gaps.subscription_gap_count == 1
    assert gaps.subscription_gaps[0]["id"] == gappy_sub.id
    assert gaps.match_rate == 0.5


# --- Wireless arm: gaps stays in sync with resolve_customer_path's radio arm ---


def _wireless_setup(db_session, subscriber_id, uisp_status="active", pop=True):
    from app.models.network import CPEDevice

    pop_site = None
    if pop:
        pop_site = PopSite(name=f"BTS-{uuid.uuid4().hex[:6]}", zabbix_group_id="40")
        db_session.add(pop_site)
        db_session.flush()
    ap = NetworkDevice(
        name=f"ap-{uuid.uuid4().hex[:6]}",
        pop_site_id=pop_site.id if pop_site else None,
        uisp_device_id=f"uisp-{uuid.uuid4().hex[:6]}",
    )
    db_session.add(ap)
    db_session.flush()
    db_session.add(
        CPEDevice(
            subscriber_id=subscriber_id,
            parent_network_device_id=ap.id,
            last_uisp_status=uisp_status,
        )
    )
    db_session.flush()


def test_wireless_only_subscriber_is_not_a_gap(db_session, subscriber, catalog_offer):
    # Radio -> AP -> pop_site: complete path, no gap, counted in match-rate.
    _wireless_setup(db_session, subscriber.id)
    db_session.add(_sub(subscriber.id, catalog_offer.id))
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.active_subscriptions == 1
    assert gaps.subscription_gap_count == 0
    assert gaps.resolved_complete == 1
    assert gaps.match_rate == 1.0


def test_vanished_cpe_only_subscriber_is_still_a_gap(
    db_session, subscriber, catalog_offer
):
    # A vanished radio does not resolve a path (mirrors resolve_customer_path,
    # which skips it and, with no NAS, lands on GAP_NO_ONT).
    _wireless_setup(db_session, subscriber.id, uisp_status="vanished")
    db_session.add(_sub(subscriber.id, catalog_offer.id))
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.subscription_gap_count == 1
    assert gaps.subscription_gaps[0]["gap"] == "no_ont"


def test_wireless_subscriber_ap_without_basestation_is_gap_no_basestation(
    db_session, subscriber, catalog_offer
):
    _wireless_setup(db_session, subscriber.id, pop=False)
    db_session.add(_sub(subscriber.id, catalog_offer.id))
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.subscription_gap_count == 1
    assert gaps.subscription_gaps[0]["gap"] == "no_basestation"
