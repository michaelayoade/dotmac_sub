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
