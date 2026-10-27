"""Behavior tests for IPv6 service-linking and the service-link unlink inverse.

IPv6 linking mirrors the ``ready_link`` branch of
``repair_service_ipv4_assignment``: it links an existing active IPv6
assignment only (no create, release, or deactivation) and never touches the
served IPv6 column. IPv6 has no primary-served marker -- ``is_primary`` and
its partial-unique index are IPv4-only -- so linking must not set it.

Unlinking is the rollback inverse for links made by the ownership/lifecycle
repairs: it clears ``subscription_id`` (and the IPv4 primary marker) after a
fingerprinted preview, and never deactivates or releases the address.
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
    IPv6Address,
    IPVersion,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Address, AddressType, Subscriber
from app.services.ip_assignment_lifecycle import (
    IPAssignmentOwnershipError,
    IPAssignmentServiceUnlinkDecision,
    IPv4AssignmentLifecycleError,
    IPv4AssignmentRepairDecision,
    IPv6AssignmentLinkDecision,
    LinkServiceIPv6AssignmentCommand,
    RepairServiceIPv4AssignmentCommand,
    UnlinkIPAssignmentServiceOwnershipCommand,
    link_service_ipv6_assignment,
    preview_ip_assignment_service_unlink,
    preview_service_ipv4_assignment_repair,
    preview_service_ipv6_assignment_link,
    repair_service_ipv4_assignment,
    unlink_ip_assignment_service_ownership,
)
from app.services.owner_commands import CommandContext


def _subscriber(db, suffix: str) -> Subscriber:
    subscriber = Subscriber(
        first_name="IPAM",
        last_name="IPv6Link",
        email=f"ipam-ipv6-{suffix}-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _subscription(
    db,
    subscriber: Subscriber,
    offer,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
    served_ipv6: str | None = None,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=f"ipam-ipv6-{uuid4().hex[:10]}",
        ipv6_address=served_ipv6,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _ipv6_pool(db, suffix: str) -> IpPool:
    pool = IpPool(
        name=f"Subscriber IPv6 Pool {suffix}",
        ip_version=IPVersion.ipv6,
        cidr="2001:db8:30::/48",
        is_active=True,
    )
    db.add(pool)
    db.flush()
    return pool


def _ipv6_address(db, pool: IpPool, value: str) -> IPv6Address:
    address = IPv6Address(address=value, pool_id=pool.id, is_reserved=False)
    db.add(address)
    db.flush()
    return address


def _ipv6_assignment(
    db,
    subscriber: Subscriber,
    address: IPv6Address,
    *,
    subscription: Subscription | None = None,
) -> IPAssignment:
    assignment = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id if subscription is not None else None,
        ip_version=IPVersion.ipv6,
        ipv6_address_id=address.id,
        is_active=True,
    )
    db.add(assignment)
    db.flush()
    return assignment


def _link_command(
    *,
    subscription_id: UUID,
    desired_address_id: UUID,
    fingerprint: str,
    key: str = "ipam-ipv6-link-test-1",
) -> LinkServiceIPv6AssignmentCommand:
    return LinkServiceIPv6AssignmentCommand(
        context=CommandContext.system(
            actor="test-operator",
            scope="ip_assignment_lifecycle_repair",
            reason="Reviewed exact-service IPv6 link evidence",
            idempotency_key=key,
        ),
        subscription_id=subscription_id,
        desired_address_id=desired_address_id,
        preview_fingerprint=fingerprint,
    )


def _unlink_command(
    *,
    assignment_id: UUID,
    expected_subscription_id: UUID,
    fingerprint: str,
    key: str = "ipam-unlink-test-1",
) -> UnlinkIPAssignmentServiceOwnershipCommand:
    return UnlinkIPAssignmentServiceOwnershipCommand(
        context=CommandContext.system(
            actor="test-operator",
            scope="ipam_service_ownership_unlink",
            reason="Reviewed service-link rollback evidence",
            idempotency_key=key,
        ),
        assignment_id=assignment_id,
        expected_subscription_id=expected_subscription_id,
        preview_fingerprint=fingerprint,
    )


def test_ipv6_link_links_legacy_assignment_without_primary_or_served_write(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "link")
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        served_ipv6="2001:db8:30::99",
    )
    pool = _ipv6_pool(db_session, "link")
    desired = _ipv6_address(db_session, pool, "2001:db8:30::10")
    assignment = _ipv6_assignment(db_session, subscriber, desired)
    subscription_id = subscription.id
    desired_id = desired.id
    db_session.commit()

    preview = preview_service_ipv6_assignment_link(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
    )
    assert preview.decision is IPv6AssignmentLinkDecision.ready_link
    assert preview.applicable is True
    db_session.commit()

    command = _link_command(
        subscription_id=subscription_id,
        desired_address_id=desired_id,
        fingerprint=preview.fingerprint,
    )
    outcome = link_service_ipv6_assignment(db_session, command)

    assert outcome.linked_count == 1
    assert outcome.replayed is False
    assert outcome.desired_assignment_id == assignment.id
    assert outcome.subscription_id == subscription_id
    db_session.refresh(assignment)
    db_session.refresh(subscription)
    assert assignment.subscription_id == subscription_id
    assert assignment.is_active is True
    # IPv6 has no primary-served marker; linking must not invent one.
    assert assignment.is_primary is False
    assert subscription.ipv6_address == "2001:db8:30::99"
    # New commands are typed audit-actor adopters: the staged batch row
    # carries the service actor built from the command context.
    batch_event = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "network.ip_assignment.ipv6_lifecycle_repaired",
            AuditEvent.entity_id == "ipam-ipv6-link-test-1",
        )
    )
    assert batch_event is not None
    assert batch_event.actor_type.value == "service"
    assert batch_event.actor_id == "test-operator"

    db_session.commit()
    # Replay takes subscription_id from the command (bound by the preview
    # fingerprint), not from audit metadata.
    replay = link_service_ipv6_assignment(db_session, command)
    assert replay.replayed is True
    assert replay.subscription_id == subscription_id
    assert replay.desired_assignment_id == assignment.id
    assert replay.linked_count == 1


def test_ipv6_link_preview_refuses_cross_customer_target(
    db_session,
    catalog_offer,
) -> None:
    owner = _subscriber(db_session, "owner")
    owner_subscription = _subscription(db_session, owner, catalog_offer)
    target = _subscriber(db_session, "target")
    target_subscription = _subscription(db_session, target, catalog_offer)
    pool = _ipv6_pool(db_session, "conflict")
    address = _ipv6_address(db_session, pool, "2001:db8:30::20")
    _ipv6_assignment(db_session, owner, address, subscription=owner_subscription)
    db_session.commit()

    preview = preview_service_ipv6_assignment_link(
        db_session,
        subscription_id=target_subscription.id,
        desired_address_id=address.id,
    )

    assert preview.decision is IPv6AssignmentLinkDecision.target_owned_by_other_service
    assert preview.applicable is False


def test_ipv6_link_preview_refuses_service_already_served_on_ipv6(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "served")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _ipv6_pool(db_session, "served")
    served = _ipv6_address(db_session, pool, "2001:db8:30::30")
    surplus = _ipv6_address(db_session, pool, "2001:db8:30::31")
    _ipv6_assignment(db_session, subscriber, served, subscription=subscription)
    _ipv6_assignment(db_session, subscriber, surplus)
    db_session.commit()

    preview = preview_service_ipv6_assignment_link(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=surplus.id,
    )

    assert (
        preview.decision
        is IPv6AssignmentLinkDecision.service_already_has_ipv6_assignment
    )
    assert preview.applicable is False


def test_ipv6_link_preview_refuses_terminal_subscription(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "terminal")
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.canceled,
    )
    pool = _ipv6_pool(db_session, "terminal")
    address = _ipv6_address(db_session, pool, "2001:db8:30::40")
    _ipv6_assignment(db_session, subscriber, address)
    db_session.commit()

    preview = preview_service_ipv6_assignment_link(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=address.id,
    )

    assert preview.decision is IPv6AssignmentLinkDecision.subscription_not_serviceable
    assert preview.applicable is False


def _link_ipv4_assignment(db_session, catalog_offer, suffix: str):
    """Create an exact primary IPv4 link through the existing IPv4 repair."""
    subscriber = _subscriber(db_session, suffix)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = IpPool(
        name=f"Subscriber Pool {suffix}",
        ip_version=IPVersion.ipv4,
        cidr="10.30.0.0/24",
        is_active=True,
    )
    db_session.add(pool)
    db_session.flush()
    address = IPv4Address(
        address="10.30.0.50", pool_id=pool.id, allocation_type="static"
    )
    db_session.add(address)
    db_session.flush()
    assignment = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=None,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    subscription_id = subscription.id
    address_id = address.id
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=address_id,
    )
    assert preview.decision is IPv4AssignmentRepairDecision.ready_link
    db_session.commit()
    repair_service_ipv4_assignment(
        db_session,
        RepairServiceIPv4AssignmentCommand(
            context=CommandContext.system(
                actor="test-operator",
                scope="ip_assignment_lifecycle_repair",
                reason="Reviewed exact-service IPAM lifecycle evidence",
                idempotency_key=f"ipam-v4-link-{suffix}",
            ),
            subscription_id=subscription_id,
            desired_address_id=address_id,
            deactivate_assignment_ids=(),
            preview_fingerprint=preview.fingerprint,
        ),
    )
    db_session.refresh(assignment)
    assert assignment.subscription_id == subscription_id
    assert assignment.is_primary is True
    return assignment, subscription_id


def test_unlink_round_trip_ipv4_keeps_address_active(
    db_session,
    catalog_offer,
) -> None:
    assignment, subscription_id = _link_ipv4_assignment(
        db_session, catalog_offer, "unlink4"
    )
    assignment_id = assignment.id

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    db_session.commit()

    command = _unlink_command(
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
        fingerprint=preview.fingerprint,
        key="ipam-unlink-v4",
    )
    outcome = unlink_ip_assignment_service_ownership(db_session, command)

    assert outcome.replayed is False
    assert outcome.previous_subscription_id == subscription_id
    db_session.refresh(assignment)
    assert assignment.subscription_id is None
    assert assignment.is_primary is False
    assert assignment.is_active is True
    assert assignment.ipv4_address_id is not None
    actions = set(
        db_session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_id.in_((str(assignment_id), "ipam-unlink-v4"))
            )
        ).all()
    )
    assert "network.ip_assignment.service_ownership_unlinked" in actions

    db_session.commit()
    replay = unlink_ip_assignment_service_ownership(db_session, command)
    assert replay.replayed is True


def test_unlink_round_trip_ipv6(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "unlink6")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _ipv6_pool(db_session, "unlink6")
    desired = _ipv6_address(db_session, pool, "2001:db8:30::60")
    assignment = _ipv6_assignment(db_session, subscriber, desired)
    subscription_id = subscription.id
    assignment_id = assignment.id
    desired_id = desired.id
    db_session.commit()

    link_preview = preview_service_ipv6_assignment_link(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
    )
    db_session.commit()
    link_service_ipv6_assignment(
        db_session,
        _link_command(
            subscription_id=subscription_id,
            desired_address_id=desired_id,
            fingerprint=link_preview.fingerprint,
            key="ipam-ipv6-link-unlink6",
        ),
    )

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    db_session.commit()
    outcome = unlink_ip_assignment_service_ownership(
        db_session,
        _unlink_command(
            assignment_id=assignment_id,
            expected_subscription_id=subscription_id,
            fingerprint=preview.fingerprint,
            key="ipam-unlink-v6",
        ),
    )

    assert outcome.replayed is False
    db_session.refresh(assignment)
    assert assignment.subscription_id is None
    assert assignment.is_active is True
    assert assignment.ipv6_address_id == desired.id


def test_unlink_refuses_when_link_state_moved_after_preview(
    db_session,
    catalog_offer,
) -> None:
    assignment, subscription_id = _link_ipv4_assignment(
        db_session, catalog_offer, "moved"
    )
    other_subscriber = _subscriber(db_session, "moved-other")
    other_subscription = _subscription(db_session, other_subscriber, catalog_offer)
    other_subscription_id = other_subscription.id
    assignment_id = assignment.id
    db_session.commit()

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    db_session.commit()

    moved = db_session.get(IPAssignment, assignment_id)
    assert moved is not None
    moved.subscription_id = other_subscription_id
    db_session.commit()

    with pytest.raises(IPAssignmentOwnershipError) as exc_info:
        unlink_ip_assignment_service_ownership(
            db_session,
            _unlink_command(
                assignment_id=assignment_id,
                expected_subscription_id=subscription_id,
                fingerprint=preview.fingerprint,
                key="ipam-unlink-moved",
            ),
        )
    assert exc_info.value.code.endswith(".stale_preview")
    db_session.rollback()
    db_session.refresh(assignment)
    assert assignment.subscription_id == other_subscription_id
    assert assignment.is_active is True


def test_unlink_preview_refuses_wrong_expected_subscription_and_unlinked(
    db_session,
    catalog_offer,
) -> None:
    assignment, subscription_id = _link_ipv4_assignment(
        db_session, catalog_offer, "wrong"
    )
    other_subscriber = _subscriber(db_session, "wrong-other")
    other_subscription = _subscription(db_session, other_subscriber, catalog_offer)
    unbound_address = IPv4Address(address="10.30.0.51", allocation_type="static")
    db_session.add(unbound_address)
    db_session.flush()
    unbound = IPAssignment(
        subscriber_id=other_subscriber.id,
        subscription_id=None,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=unbound_address.id,
        is_active=True,
    )
    db_session.add(unbound)
    db_session.flush()
    db_session.commit()

    wrong = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment.id,
        expected_subscription_id=other_subscription.id,
    )
    assert wrong.decision is IPAssignmentServiceUnlinkDecision.subscription_mismatch
    assert wrong.applicable is False

    unlinked = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=unbound.id,
        expected_subscription_id=subscription_id,
    )
    assert unlinked.decision is IPAssignmentServiceUnlinkDecision.assignment_not_linked
    assert unlinked.applicable is False


def _service_address(db, subscriber_id: UUID) -> Address:
    address = Address(
        subscriber_id=subscriber_id,
        address_type=AddressType.service,
        label="Primary service",
        address_line1="1 Test Street",
        city="Abuja",
        region="FCT",
        country_code="NG",
    )
    db.add(address)
    db.flush()
    return address


def test_unlink_preserves_served_columns_and_clears_matching_service_address(
    db_session,
    catalog_offer,
) -> None:
    assignment, subscription_id = _link_ipv4_assignment(
        db_session, catalog_offer, "servedkeep"
    )
    subscription = db_session.get(Subscription, subscription_id)
    assert subscription is not None
    assert assignment.subscriber_id is not None
    service_address = _service_address(db_session, assignment.subscriber_id)
    subscription.ipv4_address = "10.30.0.50"
    subscription.ipv6_address = "2001:db8:30::50"
    subscription.service_address_id = service_address.id
    assignment.service_address_id = service_address.id
    assignment_id = assignment.id
    service_address_id = service_address.id
    db_session.commit()

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    assert preview.subscription_status == "active"
    # Live-use evidence: the served column for this family, normalized.
    assert preview.served_address == "10.30.0.50"
    assert preview.served_address_matches_assignment is True
    assert preview.live_session_checked is True
    assert preview.live_session_present is False
    db_session.commit()

    unlink_ip_assignment_service_ownership(
        db_session,
        _unlink_command(
            assignment_id=assignment_id,
            expected_subscription_id=subscription_id,
            fingerprint=preview.fingerprint,
            key="ipam-unlink-servedkeep",
        ),
    )

    db_session.refresh(assignment)
    db_session.refresh(subscription)
    assert assignment.subscription_id is None
    # Matching service address is cleared from the assignment only.
    assert assignment.service_address_id is None
    # Served columns and the subscription's own service address survive.
    assert subscription.ipv4_address == "10.30.0.50"
    assert subscription.ipv6_address == "2001:db8:30::50"
    assert subscription.service_address_id == service_address_id


def test_unlink_preserves_non_matching_service_address(
    db_session,
    catalog_offer,
) -> None:
    assignment, subscription_id = _link_ipv4_assignment(
        db_session, catalog_offer, "servedother"
    )
    subscription = db_session.get(Subscription, subscription_id)
    assert subscription is not None
    assert assignment.subscriber_id is not None
    own_address = _service_address(db_session, assignment.subscriber_id)
    other_address = _service_address(db_session, assignment.subscriber_id)
    subscription.ipv4_address = "10.30.0.99"
    subscription.service_address_id = own_address.id
    assignment.service_address_id = other_address.id
    assignment_id = assignment.id
    other_address_id = other_address.id
    db_session.commit()

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    assert preview.served_address == "10.30.0.99"
    assert preview.served_address_matches_assignment is False
    db_session.commit()

    unlink_ip_assignment_service_ownership(
        db_session,
        _unlink_command(
            assignment_id=assignment_id,
            expected_subscription_id=subscription_id,
            fingerprint=preview.fingerprint,
            key="ipam-unlink-servedother",
        ),
    )

    db_session.refresh(assignment)
    db_session.refresh(subscription)
    assert assignment.subscription_id is None
    # A service address that is not the subscription's own is left alone.
    assert assignment.service_address_id == other_address_id
    assert subscription.ipv4_address == "10.30.0.99"


def test_unlink_preview_reports_ipv4_live_session_without_refusing(
    db_session,
    catalog_offer,
) -> None:
    assignment, subscription_id = _link_ipv4_assignment(
        db_session, catalog_offer, "livesession"
    )
    db_session.add(
        RadiusActiveSession(
            subscription_id=subscription_id,
            username="ipam-live-session",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address="10.30.0.50",
            session_start=datetime.now(UTC),
        )
    )
    assignment_id = assignment.id
    db_session.commit()

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    # Evidence only: a live session never blocks the rollback.
    assert preview.live_session_checked is True
    assert preview.live_session_present is True
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    assert preview.applicable is True
    db_session.commit()

    unlink_ip_assignment_service_ownership(
        db_session,
        _unlink_command(
            assignment_id=assignment_id,
            expected_subscription_id=subscription_id,
            fingerprint=preview.fingerprint,
            key="ipam-unlink-livesession",
        ),
    )
    db_session.refresh(assignment)
    assert assignment.subscription_id is None


def test_unlink_preview_ipv6_served_evidence_without_live_session_helper(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "unlink6-served")
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        served_ipv6="2001:db8:30::61",
    )
    pool = _ipv6_pool(db_session, "unlink6-served")
    desired = _ipv6_address(db_session, pool, "2001:db8:30::61")
    assignment = _ipv6_assignment(
        db_session, subscriber, desired, subscription=subscription
    )
    assignment_id = assignment.id
    subscription_id = subscription.id
    db_session.commit()

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    assert preview.served_address == "2001:db8:30::61"
    assert preview.served_address_matches_assignment is True
    # No IPv6 equivalent of find_ipv4_live_use exists; the preview says so
    # instead of inventing session semantics for IPv6.
    assert preview.live_session_checked is False
    assert preview.live_session_present is None


def test_unlink_refuses_when_served_column_moves_after_preview(
    db_session,
    catalog_offer,
) -> None:
    assignment, subscription_id = _link_ipv4_assignment(
        db_session, catalog_offer, "servedmoved"
    )
    subscription = db_session.get(Subscription, subscription_id)
    assert subscription is not None
    subscription.ipv4_address = "10.30.0.50"
    assignment_id = assignment.id
    db_session.commit()

    preview = preview_ip_assignment_service_unlink(
        db_session,
        assignment_id=assignment_id,
        expected_subscription_id=subscription_id,
    )
    assert preview.decision is IPAssignmentServiceUnlinkDecision.ready_unlink
    db_session.commit()

    moved = db_session.get(Subscription, subscription_id)
    assert moved is not None
    moved.ipv4_address = "10.30.0.77"
    db_session.commit()

    with pytest.raises(IPAssignmentOwnershipError) as exc_info:
        unlink_ip_assignment_service_ownership(
            db_session,
            _unlink_command(
                assignment_id=assignment_id,
                expected_subscription_id=subscription_id,
                fingerprint=preview.fingerprint,
                key="ipam-unlink-servedmoved",
            ),
        )
    assert exc_info.value.code.endswith(".stale_preview")
    db_session.rollback()
    db_session.refresh(assignment)
    assert assignment.subscription_id == subscription_id
    assert assignment.is_active is True


def test_ipv6_link_apply_refuses_stale_fingerprint_and_writes_nothing(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "link-stale")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _ipv6_pool(db_session, "link-stale")
    desired = _ipv6_address(db_session, pool, "2001:db8:30::70")
    assignment = _ipv6_assignment(db_session, subscriber, desired)
    subscription_id = subscription.id
    desired_id = desired.id
    db_session.commit()

    preview = preview_service_ipv6_assignment_link(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
    )
    assert preview.decision is IPv6AssignmentLinkDecision.ready_link
    db_session.commit()

    assignment.is_active = False
    db_session.commit()

    with pytest.raises(IPv4AssignmentLifecycleError) as exc_info:
        link_service_ipv6_assignment(
            db_session,
            _link_command(
                subscription_id=subscription_id,
                desired_address_id=desired_id,
                fingerprint=preview.fingerprint,
                key="ipam-ipv6-link-stale",
            ),
        )
    assert exc_info.value.code.endswith(".stale_preview")
    db_session.rollback()
    db_session.refresh(assignment)
    assert assignment.subscription_id is None
    assert assignment.service_address_id is None


def test_ipv6_link_apply_refuses_non_applicable_preview_and_writes_nothing(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "link-unsafe")
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.canceled,
    )
    pool = _ipv6_pool(db_session, "link-unsafe")
    desired = _ipv6_address(db_session, pool, "2001:db8:30::71")
    assignment = _ipv6_assignment(db_session, subscriber, desired)
    subscription_id = subscription.id
    desired_id = desired.id
    db_session.commit()

    preview = preview_service_ipv6_assignment_link(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
    )
    assert preview.decision is IPv6AssignmentLinkDecision.subscription_not_serviceable
    assert preview.applicable is False
    db_session.commit()

    with pytest.raises(IPv4AssignmentLifecycleError) as exc_info:
        link_service_ipv6_assignment(
            db_session,
            _link_command(
                subscription_id=subscription_id,
                desired_address_id=desired_id,
                fingerprint=preview.fingerprint,
                key="ipam-ipv6-link-unsafe",
            ),
        )
    assert exc_info.value.code.endswith(".unsafe_repair")
    db_session.rollback()
    db_session.refresh(assignment)
    assert assignment.subscription_id is None
    assert assignment.service_address_id is None
    assert assignment.is_active is True
