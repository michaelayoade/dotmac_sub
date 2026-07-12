from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import DeviceStatus, NetworkDevice, PopSite
from app.models.radius_active_session import RadiusActiveSession
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services.events.types import EventType
from app.services.network.access_path import summarize_subscription_access_path
from app.services.network.device_state import resolve_device_state
from app.services.network.events import (
    decide_device_state_event,
    decide_outage_event,
    decide_radius_session_event,
)
from app.services.network.identity import identity_for_subscription
from app.services.network.outage_impact import OutageImpact
from app.services.network.radius_sessions import (
    active_session_count_for_subscriber,
    latest_open_accounting_session_for_subscription,
    latest_open_accounting_sessions_by_subscription,
    live_framed_ips_by_subscription,
    resolve_subscriber_radius_sessions,
)
from app.services.network.sot_relationships import (
    dependencies_for,
    dependency_order,
)


def _nas_node(db_session, *, pop: PopSite | None = None):
    nas = NasDevice(name=f"NAS-{uuid.uuid4().hex[:6]}", management_ip="10.0.0.1")
    db_session.add(nas)
    db_session.flush()
    node = NetworkDevice(
        name="NAS Node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        pop_site_id=pop.id if pop is not None else None,
        is_active=True,
        ping_enabled=True,
        mgmt_ip="10.0.0.1",
        live_status="up",
    )
    db_session.add(node)
    db_session.flush()
    return nas, node


def test_network_sot_relationships_are_ordered():
    assert dependency_order() == [
        "identity",
        "access_path",
        "radius_sessions",
        "device_state",
        "outage_impact",
        "events",
    ]
    assert dependencies_for("outage_impact") == ("access_path", "device_state")
    assert dependencies_for("events") == (
        "device_state",
        "outage_impact",
        "radius_sessions",
    )


def test_identity_and_access_path_resolve_subscription_nas_node(
    db_session, subscriber, catalog_offer
):
    pop = PopSite(name="POP 1", code="POP-SOT")
    db_session.add(pop)
    db_session.flush()
    nas, node = _nas_node(db_session, pop=pop)
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas.id,
    )
    db_session.add(subscription)
    db_session.flush()

    summary = summarize_subscription_access_path(db_session, subscription)
    identity = identity_for_subscription(db_session, subscription)

    assert summary.access_kind == "nas"
    assert summary.node_id == node.id
    assert summary.basestation_id == pop.id
    assert identity is not None
    assert identity.kind == "nas"
    assert identity.network_device == node
    assert identity.pop_site == pop


def test_radius_session_resolution_uses_freshest_session(
    db_session, subscriber, catalog_offer
):
    nas, _node = _nas_node(db_session)
    older = RadiusActiveSession(
        subscriber_id=subscriber.id,
        username="sot-user",
        acct_session_id="older-session",
        nas_device_id=nas.id,
        session_start=datetime.now(UTC) - timedelta(hours=2),
        last_update=datetime.now(UTC) - timedelta(minutes=10),
    )
    newer = RadiusActiveSession(
        subscriber_id=subscriber.id,
        username="sot-user",
        acct_session_id="newer-session",
        nas_device_id=nas.id,
        session_start=datetime.now(UTC) - timedelta(hours=1),
        last_update=datetime.now(UTC),
    )
    db_session.add_all([older, newer])
    db_session.flush()

    resolution = resolve_subscriber_radius_sessions(db_session, subscriber.id)

    assert resolution.is_online is True
    assert resolution.primary_session == newer
    assert resolution.primary_identity is not None
    assert resolution.primary_identity.kind == "nas"
    assert active_session_count_for_subscriber(db_session, subscriber.id) == 2


def test_open_accounting_session_helpers_use_newest_non_stop_session(
    db_session, subscriber, catalog_offer
):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)
    db_session.flush()
    older = RadiusAccountingSession(
        subscription_id=subscription.id,
        session_id="older",
        status_type=AccountingStatus.interim,
        session_start=datetime.now(UTC) - timedelta(hours=2),
        last_update_at=datetime.now(UTC) - timedelta(minutes=30),
        framed_ip_address="10.0.0.10",
    )
    newer = RadiusAccountingSession(
        subscription_id=subscription.id,
        session_id="newer",
        status_type=AccountingStatus.interim,
        session_start=datetime.now(UTC) - timedelta(hours=1),
        last_update_at=datetime.now(UTC),
        framed_ip_address="10.0.0.11",
    )
    stopped = RadiusAccountingSession(
        subscription_id=subscription.id,
        session_id="stopped",
        status_type=AccountingStatus.stop,
        session_start=datetime.now(UTC) - timedelta(minutes=5),
        last_update_at=datetime.now(UTC) + timedelta(minutes=1),
        session_end=datetime.now(UTC),
        framed_ip_address="10.0.0.12",
    )
    db_session.add_all([older, newer, stopped])
    db_session.flush()

    assert (
        latest_open_accounting_session_for_subscription(db_session, subscription.id)
        == newer
    )
    assert latest_open_accounting_sessions_by_subscription(
        db_session, [subscription.id]
    ) == {subscription.id: newer}
    assert live_framed_ips_by_subscription(db_session, [subscription.id]) == {
        subscription.id: "10.0.0.11"
    }


def test_device_state_and_event_decision_for_down_transition(db_session):
    node = NetworkDevice(
        name="Down Node",
        status=DeviceStatus.online,
        is_active=True,
        ping_enabled=True,
        mgmt_ip="10.0.0.9",
        live_status="down",
        live_status_at=datetime.now(UTC),
    )
    db_session.add(node)
    db_session.flush()

    state = resolve_device_state(db_session, node)
    decision = decide_device_state_event(previous_status="up", current=state)

    assert state.live_status == "down"
    assert state.source == "topology.live_status"
    assert decision.should_emit is True
    assert decision.event_type == EventType.device_offline
    assert decision.payload["device_id"] == str(node.id)


def test_outage_and_radius_event_decisions_are_customer_impact_driven(
    db_session, subscriber
):
    empty_impact = OutageImpact(
        scope_type="node",
        scope_id=uuid.uuid4(),
        affected_count=0,
        payload={"count": 0},
    )
    live_impact = OutageImpact(
        scope_type="node",
        scope_id=uuid.uuid4(),
        affected_count=3,
        payload={"count": 3},
    )

    assert (
        decide_outage_event(
            impact=empty_impact, alert_type="outage.created"
        ).should_emit
        is False
    )
    outage_decision = decide_outage_event(
        impact=live_impact,
        alert_type="outage.created",
    )
    session_decision = decide_radius_session_event(
        before_online=False,
        current=resolve_subscriber_radius_sessions(db_session, subscriber.id),
    )

    assert outage_decision.should_emit is True
    assert outage_decision.event_type == EventType.network_alert
    assert session_decision.should_emit is False
    assert session_decision.reason == "unchanged"
