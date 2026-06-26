"""Subscriber hard-delete releases active IP assignments + retires CPE (H1/H3)."""

from __future__ import annotations

import uuid

from app.models.network import (
    CPEDevice,
    DeviceStatus,
    IPAssignment,
    IpPool,
    IPv4Address,
    IPVersion,
)
from app.models.subscriber import Subscriber
from app.services.subscriber import _release_subscriber_network_records


def test_release_deactivates_assignment_and_retires_cpe(db_session):
    sub = Subscriber(
        first_name="A", last_name="L", email=f"{uuid.uuid4().hex[:8]}@e.com"
    )
    pool = IpPool(
        id=uuid.uuid4(),
        name="P",
        ip_version=IPVersion.ipv4,
        cidr="10.0.0.0/24",
        is_active=True,
    )
    db_session.add_all([sub, pool])
    db_session.flush()
    addr = IPv4Address(address="10.0.0.7", pool_id=pool.id)
    db_session.add(addr)
    db_session.flush()
    assignment = IPAssignment(
        subscriber_id=sub.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=addr.id,
        is_active=True,
    )
    cpe = CPEDevice(subscriber_id=sub.id, status=DeviceStatus.active)
    db_session.add_all([assignment, cpe])
    db_session.commit()

    _release_subscriber_network_records(db_session, sub.id)

    # The active assignment is released (so SET NULL never orphans a live row,
    # and the IP becomes reusable under the partial-active-unique index).
    assert db_session.get(IPAssignment, assignment.id).is_active is False
    # The active CPE is retired (no ownerless active device).
    assert db_session.get(CPEDevice, cpe.id).status == DeviceStatus.retired


def test_release_is_noop_without_active_records(db_session):
    sub = Subscriber(
        first_name="B", last_name="L", email=f"{uuid.uuid4().hex[:8]}@e.com"
    )
    db_session.add(sub)
    db_session.commit()
    # Should not raise when the subscriber has no assignments/CPE.
    _release_subscriber_network_records(db_session, sub.id)
