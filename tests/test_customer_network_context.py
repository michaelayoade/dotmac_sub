from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.network import (
    CPEDevice,
    DeviceStatus,
    DeviceType,
    IPAssignment,
    IpPool,
    IPv4Address,
    Ipv6DelegatedPrefix,
    Ipv6PrefixState,
    IPVersion,
    OntAssignment,
    OntUnit,
)
from app.models.radius_active_session import RadiusActiveSession
from app.services.customer_network_context import (
    get_customer_network_context,
    list_active_radius_sessions,
    resolve_active_customer_ont_assignment,
)


def test_customer_network_context_collects_customer_footprint(db_session, subscriber):
    ont = OntUnit(serial_number="CTX-ONT-1", model="HG8245", is_active=True)
    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        device_type=DeviceType.wireless_radio,
        status=DeviceStatus.active,
        serial_number="RADIO-1",
        mac_address="AA:BB:CC:00:00:01",
    )
    ipv4_pool = IpPool(name="ctx-v4", ip_version=IPVersion.ipv4, cidr="10.0.0.0/24")
    ipv6_pool = IpPool(
        name="ctx-v6",
        ip_version=IPVersion.ipv6,
        cidr="2001:db8::/48",
        delegation_prefix_length=64,
    )
    db_session.add_all([ont, cpe, ipv4_pool, ipv6_pool])
    db_session.flush()
    ipv4 = IPv4Address(address="10.0.0.10", pool_id=ipv4_pool.id)
    db_session.add(ipv4)
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                subscriber_id=subscriber.id,
                active=True,
                assigned_at=datetime.now(UTC),
            ),
            IPAssignment(
                subscriber_id=subscriber.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=ipv4.id,
                is_active=True,
            ),
            Ipv6DelegatedPrefix(
                pool_id=ipv6_pool.id,
                prefix="2001:db8:1::",
                prefix_length=64,
                state=Ipv6PrefixState.assigned,
                subscriber_id=subscriber.id,
            ),
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                username="ctx-user",
                acct_session_id="ctx-session",
                framed_ip_address="100.64.10.10",
                session_start=datetime.now(UTC) - timedelta(minutes=5),
                last_update=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    context = get_customer_network_context(db_session, subscriber.id)

    assert context.has_access_equipment is True
    assert context.is_online is True
    assert [a.ont_unit.serial_number for a in context.ont_assignments] == ["CTX-ONT-1"]
    assert [c.serial_number for c in context.cpe_devices] == ["RADIO-1"]
    assert context.assigned_ipv4_addresses == ("10.0.0.10",)
    assert context.delegated_prefix_cidrs == ("2001:db8:1::/64",)
    assert context.framed_ipv4_addresses == ("100.64.10.10",)


def test_customer_network_context_excludes_inactive_records(db_session, subscriber):
    ont = OntUnit(serial_number="CTX-ONT-OLD", is_active=True)
    ipv4_pool = IpPool(name="ctx-v4-old", ip_version=IPVersion.ipv4, cidr="10.1.0.0/24")
    ipv6_pool = IpPool(
        name="ctx-v6-old",
        ip_version=IPVersion.ipv6,
        cidr="2001:db8:2::/48",
    )
    db_session.add_all([ont, ipv4_pool, ipv6_pool])
    db_session.flush()
    ipv4 = IPv4Address(address="10.1.0.10", pool_id=ipv4_pool.id)
    db_session.add(ipv4)
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                subscriber_id=subscriber.id,
                active=False,
            ),
            IPAssignment(
                subscriber_id=subscriber.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=ipv4.id,
                is_active=False,
            ),
            Ipv6DelegatedPrefix(
                pool_id=ipv6_pool.id,
                prefix="2001:db8:2::",
                prefix_length=64,
                state=Ipv6PrefixState.available,
                subscriber_id=subscriber.id,
            ),
        ]
    )
    db_session.commit()

    context = get_customer_network_context(db_session, subscriber.id)

    assert context.ont_assignments == []
    assert context.active_ip_assignments == []
    assert context.delegated_prefixes == []
    assert context.has_access_equipment is False


def test_active_radius_sessions_are_freshest_first(db_session, subscriber):
    now = datetime.now(UTC)
    older = RadiusActiveSession(
        subscriber_id=subscriber.id,
        username="old",
        acct_session_id="old-session",
        framed_ip_address="100.64.0.1",
        session_start=now - timedelta(hours=2),
        last_update=now - timedelta(hours=1),
    )
    newer = RadiusActiveSession(
        subscriber_id=subscriber.id,
        username="new",
        acct_session_id="new-session",
        framed_ip_address="100.64.0.2",
        session_start=now - timedelta(hours=1),
        last_update=now,
    )
    db_session.add_all([older, newer])
    db_session.commit()

    sessions = list_active_radius_sessions(db_session, subscriber.id)

    assert [session.username for session in sessions] == ["new", "old"]


def test_active_customer_ont_assignment_ignores_inactive_ont_and_prefers_latest(
    db_session, subscriber
):
    now = datetime.now(UTC)
    inactive_ont = OntUnit(serial_number="INACTIVE-ONT", is_active=False)
    older_active_ont = OntUnit(serial_number="OLDER-ONT", is_active=True)
    latest_active_ont = OntUnit(serial_number="LATEST-ONT", is_active=True)
    db_session.add_all([inactive_ont, older_active_ont, latest_active_ont])
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=inactive_ont.id,
                subscriber_id=subscriber.id,
                active=True,
                assigned_at=now + timedelta(minutes=5),
            ),
            OntAssignment(
                ont_unit_id=older_active_ont.id,
                subscriber_id=subscriber.id,
                active=True,
                assigned_at=now,
            ),
            OntAssignment(
                ont_unit_id=latest_active_ont.id,
                subscriber_id=subscriber.id,
                active=True,
                assigned_at=now + timedelta(minutes=1),
            ),
        ]
    )
    db_session.commit()

    assignment = resolve_active_customer_ont_assignment(db_session, subscriber.id)

    assert assignment is not None
    assert assignment.ont_unit.serial_number == "LATEST-ONT"
