"""The connectivity list stays useful after equipment recovers."""

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    CustomerOutageInterval,
    DeviceRole,
    NetworkDevice,
    PopSite,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketStatus
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services.topology.outage_console import (
    ConnectivityFilter,
    ConnectivityScope,
    customer_connectivity,
    search_scopes,
)


def _node_customer(db, offer_id):
    nas = NasDevice(name="connectivity-nas", management_ip="10.9.2.1")
    db.add(nas)
    db.flush()
    site = PopSite(name="Test BTS", is_active=True)
    db.add(site)
    db.flush()
    node = NetworkDevice(
        name="Test node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        role=DeviceRole.edge,
        pop_site_id=site.id,
        is_active=True,
        ping_enabled=True,
        live_status="up",
    )
    subscriber = Subscriber(
        first_name="Ada", last_name="Uche", email=f"{uuid.uuid4().hex}@ex.com"
    )
    db.add_all([node, subscriber])
    db.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas.id,
    )
    db.add(subscription)
    db.flush()
    return site, node, subscriber, subscription


def test_healthy_node_still_lists_customer_without_live_session(
    db_session, catalog_offer
):
    site, node, _, subscription = _node_customer(db_session, catalog_offer.id)
    now = datetime.now(UTC)

    result = customer_connectivity(
        db_session, scope=ConnectivityScope.node, scope_id=node.id, now=now
    )
    assert result is not None
    assert result.equipment_status == "up"
    assert result.total_customers == 1
    assert result.without_session == 1
    assert result.rows[0].subscription_id == subscription.id
    assert result.rows[0].customer_name == "Ada Uche"
    assert result.rows[0].downtime_started_at is None
    assert result.rows[0].downtime_seconds is None

    bts = customer_connectivity(
        db_session, scope=ConnectivityScope.basestation, scope_id=site.id, now=now
    )
    assert bts is not None
    assert bts.total_customers == 1


def test_confirmed_interval_and_service_ticket_are_shown(db_session, catalog_offer):
    _, node, subscriber, subscription = _node_customer(db_session, catalog_offer.id)
    now = datetime.now(UTC)
    db_session.add(
        CustomerOutageInterval(
            incident_id=uuid.uuid4(),
            subscription_id=subscription.id,
            state="confirmed_unavailable",
            quality="exact",
            started_at=now - timedelta(minutes=75),
            scope_revision_sequence=1,
            idempotency_key=f"test:{uuid.uuid4()}",
        )
    )
    ticket = Ticket(
        subscriber_id=subscriber.id,
        title="Internet down at customer premises",
        status=TicketStatus.open.value,
        is_active=True,
    )
    db_session.add(ticket)
    db_session.flush()

    result = customer_connectivity(
        db_session,
        scope=ConnectivityScope.node,
        scope_id=node.id,
        include_tickets=True,
        now=now,
    )
    assert result is not None
    assert result.rows[0].downtime_seconds == 75 * 60
    assert result.rows[0].downtime_quality == "exact"
    assert result.rows[0].ticket_id == ticket.id
    assert result.open_service_tickets == 1

    no_ticket = customer_connectivity(
        db_session,
        scope=ConnectivityScope.node,
        scope_id=node.id,
        status_filter=ConnectivityFilter.no_ticket,
        include_tickets=True,
        now=now,
    )
    assert no_ticket is not None
    assert no_ticket.rows == ()


def test_online_session_does_not_show_stale_open_interval(db_session, catalog_offer):
    _, node, subscriber, subscription = _node_customer(db_session, catalog_offer.id)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                username="ada",
                acct_session_id=uuid.uuid4().hex,
                session_start=now,
                last_update=now,
            ),
            CustomerOutageInterval(
                incident_id=uuid.uuid4(),
                subscription_id=subscription.id,
                state="confirmed_unavailable",
                quality="exact",
                started_at=now - timedelta(minutes=30),
                scope_revision_sequence=1,
                idempotency_key=f"test:{uuid.uuid4()}",
            ),
        ]
    )
    db_session.flush()
    result = customer_connectivity(
        db_session, scope=ConnectivityScope.node, scope_id=node.id, now=now
    )
    assert result is not None
    assert result.without_session == 0
    assert result.rows[0].has_live_session is True
    assert result.rows[0].downtime_started_at is None


def test_last_recorded_disconnect_is_labelled_estimated(db_session, catalog_offer):
    _, node, _, subscription = _node_customer(db_session, catalog_offer.id)
    now = datetime.now(UTC)
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id=uuid.uuid4().hex,
            status_type=AccountingStatus.stop,
            session_start=now - timedelta(hours=2),
            session_end=now - timedelta(minutes=90),
        )
    )
    db_session.flush()
    result = customer_connectivity(
        db_session, scope=ConnectivityScope.node, scope_id=node.id, now=now
    )
    assert result is not None
    assert result.rows[0].downtime_seconds == 90 * 60
    assert result.rows[0].downtime_quality == "estimated_from_last_disconnect"


def test_search_includes_healthy_assets(db_session):
    db_session.add_all(
        [
            PopSite(name="Healthy BTS", is_active=True),
            FdhCabinet(name="Healthy cabinet", is_active=True),
            NetworkDevice(name="Healthy node", role=DeviceRole.edge, is_active=True),
        ]
    )
    db_session.flush()
    for scope, term in (
        (ConnectivityScope.basestation, "Healthy BTS"),
        (ConnectivityScope.cabinet, "Healthy cabinet"),
        (ConnectivityScope.node, "Healthy node"),
    ):
        assert any(
            choice.name == term
            for choice in search_scopes(db_session, scope=scope, query=term)
        )
