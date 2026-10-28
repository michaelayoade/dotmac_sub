"""Behavior tests for release_unbound_ip_assignment (W0 N9 named exception).

Every eligibility class and every refusal is exercised. Addresses use the
RFC 5737 documentation range.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    IPAssignment,
    IpPool,
    IPv4Address,
    IPVersion,
    SubscriberAdditionalRoute,
)
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.ip_assignment_lifecycle import (
    IPAssignmentOwnershipError,
    ReleaseUnboundIPAssignmentCommand,
    UnboundIPv4ReleaseClass,
    UnboundIPv4ReleaseDecision,
    preview_unbound_ip_assignment_release,
    release_unbound_ip_assignment,
)
from app.services.owner_commands import CommandContext, OwnerCommandError

TERMINAL_ONLY = UnboundIPv4ReleaseClass.terminal_only_subscriber
NOT_SERVICEABLE = UnboundIPv4ReleaseClass.address_not_serviceable
ELSEWHERE = UnboundIPv4ReleaseClass.served_elsewhere_no_sibling
D = UnboundIPv4ReleaseDecision


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="IPAM",
        last_name="UnboundRelease",
        email=f"ipam-unbound-{uuid4().hex[:10]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _subscription(
    db, subscriber, offer, *, status=SubscriptionStatus.canceled, served_ip=None
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=f"ipam-unbound-{uuid4().hex[:10]}",
        ipv4_address=served_ip,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _pool(db) -> IpPool:
    pool = IpPool(
        name=f"Subscriber Pool {uuid4().hex[:6]}",
        ip_version=IPVersion.ipv4,
        cidr="198.51.100.0/24",
        is_active=True,
    )
    db.add(pool)
    db.flush()
    return pool


def _address(db, pool, value, *, reserved=False) -> IPv4Address:
    row = IPv4Address(
        address=value, pool_id=pool.id, is_reserved=reserved, allocation_type="static"
    )
    db.add(row)
    db.flush()
    return row


def _assignment(db, subscriber, address, *, subscription=None) -> IPAssignment:
    row = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id if subscription is not None else None,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def _command(
    assignment_id: UUID,
    klass: UnboundIPv4ReleaseClass,
    fingerprint: str,
    key: str = "ipam-unbound-release-1",
) -> ReleaseUnboundIPAssignmentCommand:
    return ReleaseUnboundIPAssignmentCommand(
        context=CommandContext.system(
            actor="test-operator",
            scope="ipam_unbound_assignment_release",
            reason="Reviewed N9 exception",
            idempotency_key=key,
        ),
        assignment_id=assignment_id,
        eligibility_class=klass,
        preview_fingerprint=fingerprint,
    )


def _preview(db, assignment_id, klass):
    db.commit()
    result = preview_unbound_ip_assignment_release(
        db, assignment_id=assignment_id, eligibility_class=klass
    )
    db.commit()
    return result


def _release(db, command):
    db.commit()  # owner commands need a transaction-free session
    return release_unbound_ip_assignment(db, command)


@pytest.fixture()
def world(db_session, catalog_offer):
    """A subscriber with one unbound assignment on a plain customer address."""
    subscriber = _subscriber(db_session)
    pool = _pool(db_session)
    address = _address(db_session, pool, "198.51.100.20")
    assignment = _assignment(db_session, subscriber, address)
    db_session.commit()
    return db_session, catalog_offer, subscriber, pool, address, assignment


# --- terminal_only_subscriber -------------------------------------------------


def test_terminal_only_with_no_services_is_ready_and_applies(world) -> None:
    db, _, _, _, address, assignment = world
    preview = _preview(db, assignment.id, TERMINAL_ONLY)
    assert preview.decision is D.ready_release
    assert preview.applicable
    assert db.get(IPAssignment, assignment.id).is_active is True  # preview wrote 0

    outcome = _release(db, _command(assignment.id, TERMINAL_ONLY, preview.fingerprint))
    assert outcome.released_count == 1
    assert outcome.replayed is False
    db.refresh(assignment)
    assert assignment.is_active is False
    # Only the assignment moved: the address row is untouched.
    db.refresh(address)
    assert address.is_reserved is False
    actions = set(
        db.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_id.in_([str(assignment.id), "ipam-unbound-release-1"])
            )
        )
    )
    assert {
        "network.ip_assignment.unbound_released",
        "network.ip_assignment.unbound_released_batch",
    } <= actions


def test_terminal_only_with_only_terminal_services_is_ready(world) -> None:
    db, offer, subscriber, _, _, assignment = world
    _subscription(db, subscriber, offer, status=SubscriptionStatus.canceled)
    _subscription(db, subscriber, offer, status=SubscriptionStatus.expired)
    assert _preview(db, assignment.id, TERMINAL_ONLY).decision is D.ready_release


def test_terminal_only_does_not_touch_terminal_service_column(world) -> None:
    db, offer, subscriber, _, _, assignment = world
    sub = _subscription(
        db,
        subscriber,
        offer,
        status=SubscriptionStatus.canceled,
        served_ip="198.51.100.20",
    )
    preview = _preview(db, assignment.id, TERMINAL_ONLY)
    assert preview.decision is D.ready_release
    _release(db, _command(assignment.id, TERMINAL_ONLY, preview.fingerprint))
    db.refresh(sub)
    assert sub.ipv4_address == "198.51.100.20"


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.active,
        SubscriptionStatus.disabled,
        SubscriptionStatus.suspended,
        SubscriptionStatus.blocked,
        SubscriptionStatus.archived,
    ],
)
def test_terminal_only_refuses_any_non_terminal_service(world, status) -> None:
    db, offer, subscriber, _, _, assignment = world
    _subscription(db, subscriber, offer, status=status)
    assert (
        _preview(db, assignment.id, TERMINAL_ONLY).decision
        is D.subscriber_has_non_terminal_service
    )


def test_terminal_only_refuses_reserved_address(world) -> None:
    db, _, _, _, address, assignment = world
    address.is_reserved = True
    assert (
        _preview(db, assignment.id, TERMINAL_ONLY).decision is D.address_not_releasable
    )


def test_terminal_only_refuses_network_address(db_session) -> None:
    subscriber = _subscriber(db_session)
    pool = _pool(db_session)
    address = _address(db_session, pool, "198.51.100.0")
    assignment = _assignment(db_session, subscriber, address)
    assert (
        _preview(db_session, assignment.id, TERMINAL_ONLY).decision
        is D.address_not_releasable
    )


def test_terminal_only_refuses_device_host(world) -> None:
    db, _, _, _, _, assignment = world
    db.add(NetworkDevice(name="dev", mgmt_ip="198.51.100.20", is_active=True))
    assert (
        _preview(db, assignment.id, TERMINAL_ONLY).decision is D.address_is_device_host
    )


def test_terminal_only_refuses_routed_block(world) -> None:
    db, _, subscriber, _, _, assignment = world
    db.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            cidr="198.51.100.16/29",
            prefix_length=29,
            metric=1,
            is_active=True,
            source="manual",
        )
    )
    assert (
        _preview(db, assignment.id, TERMINAL_ONLY).decision is D.address_in_routed_block
    )


def test_terminal_only_refuses_live_session(world) -> None:
    db, _, _, _, _, assignment = world
    db.add(
        RadiusActiveSession(
            username="ipam-live",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address="198.51.100.20",
            session_start=datetime.now(UTC),
        )
    )
    assert _preview(db, assignment.id, TERMINAL_ONLY).decision is D.address_in_live_use


def test_terminal_only_refuses_other_subscribers_column_holder(world) -> None:
    db, offer, _, _, _, assignment = world
    other = _subscriber(db)
    _subscription(
        db, other, offer, status=SubscriptionStatus.disabled, served_ip="198.51.100.20"
    )
    assert _preview(db, assignment.id, TERMINAL_ONLY).decision is D.address_in_live_use


# --- address_not_serviceable --------------------------------------------------


def test_not_serviceable_network_address_is_ready(db_session) -> None:
    subscriber = _subscriber(db_session)
    pool = _pool(db_session)
    address = _address(db_session, pool, "198.51.100.0")
    assignment = _assignment(db_session, subscriber, address)
    preview = _preview(db_session, assignment.id, NOT_SERVICEABLE)
    assert preview.decision is D.ready_release
    assert preview.address_is_network_or_broadcast is True


def test_not_serviceable_broadcast_address_is_ready(db_session) -> None:
    subscriber = _subscriber(db_session)
    pool = _pool(db_session)
    address = _address(db_session, pool, "198.51.100.255")
    assignment = _assignment(db_session, subscriber, address)
    assert (
        _preview(db_session, assignment.id, NOT_SERVICEABLE).decision is D.ready_release
    )


def test_not_serviceable_applies_even_with_a_retaining_service(
    db_session, catalog_offer
) -> None:
    subscriber = _subscriber(db_session)
    _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.active
    )
    pool = _pool(db_session)
    address = _address(db_session, pool, "198.51.100.0")
    assignment = _assignment(db_session, subscriber, address)
    preview = _preview(db_session, assignment.id, NOT_SERVICEABLE)
    assert preview.decision is D.ready_release
    _release(db_session, _command(assignment.id, NOT_SERVICEABLE, preview.fingerprint))
    db_session.refresh(assignment)
    assert assignment.is_active is False


def test_not_serviceable_reserved_device_host_is_ready(world) -> None:
    db, _, _, _, address, assignment = world
    address.is_reserved = True
    db.add(NetworkDevice(name="dev", mgmt_ip="198.51.100.20", is_active=True))
    assert _preview(db, assignment.id, NOT_SERVICEABLE).decision is D.ready_release


def test_not_serviceable_unreserved_device_host_is_refused(world) -> None:
    db, _, _, _, _, assignment = world
    db.add(NetworkDevice(name="dev", mgmt_ip="198.51.100.20", is_active=True))
    assert (
        _preview(db, assignment.id, NOT_SERVICEABLE).decision
        is D.release_would_free_unprotected_address
    )


def test_not_serviceable_refuses_ordinary_customer_address(world) -> None:
    db, _, _, _, _, assignment = world
    assert (
        _preview(db, assignment.id, NOT_SERVICEABLE).decision
        is D.address_is_serviceable
    )


def test_not_serviceable_refuses_live_session(db_session) -> None:
    subscriber = _subscriber(db_session)
    pool = _pool(db_session)
    address = _address(db_session, pool, "198.51.100.0")
    assignment = _assignment(db_session, subscriber, address)
    db_session.add(
        RadiusActiveSession(
            username="ipam-live",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address="198.51.100.0",
            session_start=datetime.now(UTC),
        )
    )
    assert (
        _preview(db_session, assignment.id, NOT_SERVICEABLE).decision
        is D.address_in_live_use
    )


# --- served_elsewhere_no_sibling ---------------------------------------------


def _served_elsewhere(db, offer, subscriber, pool):
    """A retaining service that already holds a different served IP exactly."""
    sub = _subscription(
        db,
        subscriber,
        offer,
        status=SubscriptionStatus.disabled,
        served_ip="198.51.100.30",
    )
    other = _address(db, pool, "198.51.100.30")
    _assignment(db, subscriber, other, subscription=sub)
    return sub


def test_served_elsewhere_ready_and_applies(world) -> None:
    db, offer, subscriber, pool, _, assignment = world
    sub = _served_elsewhere(db, offer, subscriber, pool)
    preview = _preview(db, assignment.id, ELSEWHERE)
    assert preview.decision is D.ready_release
    _release(db, _command(assignment.id, ELSEWHERE, preview.fingerprint))
    db.refresh(assignment)
    assert assignment.is_active is False
    db.refresh(sub)
    assert sub.ipv4_address == "198.51.100.30"  # customer keeps served IP


def test_served_elsewhere_refuses_uniquely_matching_sibling(world) -> None:
    db, offer, subscriber, pool, _, assignment = world
    _served_elsewhere(db, offer, subscriber, pool)
    _subscription(
        db,
        subscriber,
        offer,
        status=SubscriptionStatus.suspended,
        served_ip="198.51.100.20",
    )
    assert _preview(db, assignment.id, ELSEWHERE).decision is D.linkable_sibling_exists


def test_served_elsewhere_refuses_ambiguous_siblings(world) -> None:
    db, offer, subscriber, pool, _, assignment = world
    _served_elsewhere(db, offer, subscriber, pool)
    for _ in range(2):
        _subscription(
            db,
            subscriber,
            offer,
            status=SubscriptionStatus.suspended,
            served_ip="198.51.100.20",
        )
    assert (
        _preview(db, assignment.id, ELSEWHERE).decision
        is D.ambiguous_sibling_column_match
    )


def test_served_elsewhere_refuses_service_without_exact_assignment(world) -> None:
    db, offer, subscriber, _, _, assignment = world
    _subscription(db, subscriber, offer, status=SubscriptionStatus.disabled)
    assert (
        _preview(db, assignment.id, ELSEWHERE).decision
        is D.service_without_exact_assignment
    )


def test_served_elsewhere_refuses_when_no_non_terminal_service(world) -> None:
    db, _, _, _, _, assignment = world
    assert _preview(db, assignment.id, ELSEWHERE).decision is D.no_non_terminal_service


def test_served_elsewhere_refuses_other_subscribers_column_holder(world) -> None:
    db, offer, subscriber, pool, _, assignment = world
    _served_elsewhere(db, offer, subscriber, pool)
    other = _subscriber(db)
    _subscription(
        db, other, offer, status=SubscriptionStatus.disabled, served_ip="198.51.100.20"
    )
    assert _preview(db, assignment.id, ELSEWHERE).decision is D.address_in_live_use


def test_served_elsewhere_refuses_live_session(world) -> None:
    db, offer, subscriber, pool, _, assignment = world
    _served_elsewhere(db, offer, subscriber, pool)
    db.add(
        RadiusActiveSession(
            username="ipam-live",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address="198.51.100.20",
            session_start=datetime.now(UTC),
        )
    )
    assert _preview(db, assignment.id, ELSEWHERE).decision is D.address_in_live_use


def test_served_elsewhere_refuses_non_customer_address(
    db_session, catalog_offer
) -> None:
    subscriber = _subscriber(db_session)
    pool = _pool(db_session)
    address = _address(db_session, pool, "198.51.100.20", reserved=True)
    assignment = _assignment(db_session, subscriber, address)
    _served_elsewhere(db_session, catalog_offer, subscriber, pool)
    assert (
        _preview(db_session, assignment.id, ELSEWHERE).decision
        is D.address_not_releasable
    )


# --- class naming and row-state refusals -------------------------------------


def test_wrong_named_class_is_refused_not_coerced(world) -> None:
    """A row that only qualifies for one class is refused under another."""
    db, offer, subscriber, pool, _, assignment = world
    _served_elsewhere(db, offer, subscriber, pool)
    assert (
        _preview(db, assignment.id, TERMINAL_ONLY).decision
        is D.subscriber_has_non_terminal_service
    )
    assert (
        _preview(db, assignment.id, NOT_SERVICEABLE).decision
        is D.address_is_serviceable
    )


def test_missing_assignment(db_session) -> None:
    assert (
        _preview(db_session, uuid4(), TERMINAL_ONLY).decision is D.assignment_not_found
    )


def test_inactive_assignment(world) -> None:
    db, _, _, _, _, assignment = world
    assignment.is_active = False
    assert (
        _preview(db, assignment.id, TERMINAL_ONLY).decision is D.assignment_not_active
    )


def test_already_bound_assignment(world) -> None:
    db, offer, subscriber, _, _, assignment = world
    sub = _subscription(db, subscriber, offer, status=SubscriptionStatus.active)
    assignment.subscription_id = sub.id
    assert (
        _preview(db, assignment.id, NOT_SERVICEABLE).decision
        is D.assignment_already_bound
    )


def test_primary_assignment(world) -> None:
    db, _, _, _, _, assignment = world
    assignment.is_primary = True
    assert (
        _preview(db, assignment.id, TERMINAL_ONLY).decision is D.assignment_is_primary
    )


def test_assignment_without_subscriber(world) -> None:
    db, _, _, _, _, assignment = world
    assignment.subscriber_id = None
    assert _preview(db, assignment.id, TERMINAL_ONLY).decision is D.subscriber_not_found


def test_ipv6_assignment_is_refused(db_session) -> None:
    from app.models.network import IPv6Address

    subscriber = _subscriber(db_session)
    pool = IpPool(
        name="v6", ip_version=IPVersion.ipv6, cidr="2001:db8:30::/48", is_active=True
    )
    db_session.add(pool)
    db_session.flush()
    v6 = IPv6Address(address="2001:db8:30::10", pool_id=pool.id, is_reserved=False)
    db_session.add(v6)
    db_session.flush()
    row = IPAssignment(
        subscriber_id=subscriber.id,
        ip_version=IPVersion.ipv6,
        ipv6_address_id=v6.id,
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()
    assert _preview(db_session, row.id, TERMINAL_ONLY).decision is D.assignment_not_ipv4


# --- apply guards ------------------------------------------------------------


def test_apply_refuses_stale_fingerprint(world) -> None:
    db, offer, subscriber, _, _, assignment = world
    preview = _preview(db, assignment.id, TERMINAL_ONLY)
    _subscription(db, subscriber, offer, status=SubscriptionStatus.active)
    db.commit()
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _release(db, _command(assignment.id, TERMINAL_ONLY, preview.fingerprint))
    assert exc.value.code.endswith("stale_preview")
    db.rollback()
    assert db.get(IPAssignment, assignment.id).is_active is True


def test_apply_refuses_fingerprint_for_a_different_class(world) -> None:
    db, _, _, _, _, assignment = world
    preview = _preview(db, assignment.id, TERMINAL_ONLY)
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _release(db, _command(assignment.id, NOT_SERVICEABLE, preview.fingerprint))
    assert exc.value.code.endswith("stale_preview")


def test_apply_refuses_inapplicable_preview(world) -> None:
    db, offer, subscriber, _, _, assignment = world
    _subscription(db, subscriber, offer, status=SubscriptionStatus.active)
    preview = _preview(db, assignment.id, TERMINAL_ONLY)
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _release(db, _command(assignment.id, TERMINAL_ONLY, preview.fingerprint))
    assert exc.value.code.endswith("unsafe_cohort")
    db.rollback()
    assert db.get(IPAssignment, assignment.id).is_active is True


def test_apply_requires_idempotency_key(world) -> None:
    db, _, _, _, _, assignment = world
    preview = _preview(db, assignment.id, TERMINAL_ONLY)
    command = ReleaseUnboundIPAssignmentCommand(
        context=CommandContext.system(
            actor="test-operator",
            scope="ipam_unbound_assignment_release",
            reason="r",
            idempotency_key="  ",
        ),
        assignment_id=assignment.id,
        eligibility_class=TERMINAL_ONLY,
        preview_fingerprint=preview.fingerprint,
    )
    with pytest.raises(OwnerCommandError, match="idempotency_key"):
        _release(db, command)
    db.rollback()
    assert db.get(IPAssignment, assignment.id).is_active is True


def test_replay_is_idempotent_and_conflicting_reuse_is_refused(world) -> None:
    db, offer, subscriber, _, _, assignment = world
    preview = _preview(db, assignment.id, TERMINAL_ONLY)
    first = _release(db, _command(assignment.id, TERMINAL_ONLY, preview.fingerprint))
    assert first.replayed is False
    again = _release(db, _command(assignment.id, TERMINAL_ONLY, preview.fingerprint))
    assert again.replayed is True
    assert again.released_count == 0
    batch_rows = db.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "network.ip_assignment.unbound_released_batch"
        )
    ).all()
    assert len(batch_rows) == 1
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _release(db, _command(assignment.id, TERMINAL_ONLY, "0" * 64))
    assert exc.value.code.endswith("idempotency_conflict")


def test_release_leaves_other_assignments_and_columns_alone(world) -> None:
    db, offer, subscriber, pool, _, assignment = world
    sub = _served_elsewhere(db, offer, subscriber, pool)
    sibling_rows_before = db.scalars(
        select(IPAssignment.id).where(IPAssignment.subscription_id == sub.id)
    ).all()
    preview = _preview(db, assignment.id, ELSEWHERE)
    _release(db, _command(assignment.id, ELSEWHERE, preview.fingerprint))
    active_after = db.scalars(
        select(IPAssignment.id).where(
            IPAssignment.subscription_id == sub.id, IPAssignment.is_active.is_(True)
        )
    ).all()
    assert active_after == sibling_rows_before
