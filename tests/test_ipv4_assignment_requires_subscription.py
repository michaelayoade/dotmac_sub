"""Admin IP management must not create or produce UNBOUND IPv4 assignments.

An UNBOUND row is an ip_assignments row with an IPv4 address and a NULL
subscription_id. Device/management IPs live on the device record, so no caller
of IPAssignments.create needs one.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.schemas.network import IPAssignmentCreate, IPAssignmentUpdate
from app.services import network as network_service
from app.services import web_network_ip as ip_service
from app.services import web_network_ip_actions as actions


def _subscriber(db) -> Subscriber:
    row = Subscriber(
        first_name="Unbound",
        last_name="Guard",
        email=f"unbound-{uuid.uuid4().hex[:8]}@example.com",
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def _subscription(
    db, subscriber: Subscriber, status: SubscriptionStatus = SubscriptionStatus.active
) -> Subscription:
    offer = CatalogOffer(
        name=f"Unbound Offer {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    row = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.postpaid,
    )
    db.add(row)
    db.flush()
    return row


def _pool(db, cidr: str) -> IpPool:
    pool = IpPool(
        name=f"Unbound Pool {uuid.uuid4().hex[:8]}",
        ip_version=IPVersion.ipv4,
        cidr=cidr,
    )
    db.add(pool)
    db.flush()
    return pool


def _address(db, pool: IpPool, ip: str) -> IPv4Address:
    row = IPv4Address(address=ip, pool_id=pool.id)
    db.add(row)
    db.flush()
    return row


def _unbound_rows(db, address: IPv4Address) -> int:
    return (
        db.query(IPAssignment)
        .filter(IPAssignment.ipv4_address_id == address.id)
        .filter(IPAssignment.subscription_id.is_(None))
        .count()
    )


# --- central guard (IPAssignments.create) ----------------------------------


def test_create_refuses_ipv4_without_subscription(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    address = _address(db_session, pool, "198.51.100.10")
    subscriber = _subscriber(db_session)

    with pytest.raises(network_service.IPAssignmentError) as exc:
        network_service.ip_assignments.create(
            db_session,
            IPAssignmentCreate(
                subscriber_id=subscriber.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=address.id,
            ),
        )

    assert exc.value.code == "network.ip_assignment_lifecycle.subscription_required"
    assert "subscription_id" in exc.value.message
    assert "device record" in exc.value.message
    assert _unbound_rows(db_session, address) == 0


def test_create_refuses_ipv4_with_no_owner_at_all(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    address = _address(db_session, pool, "198.51.100.11")

    with pytest.raises(network_service.IPAssignmentError) as exc:
        network_service.ip_assignments.create(
            db_session,
            IPAssignmentCreate(ip_version=IPVersion.ipv4, ipv4_address_id=address.id),
        )

    assert exc.value.code == "network.ip_assignment_lifecycle.subscription_required"
    assert _unbound_rows(db_session, address) == 0


def test_create_with_subscription_still_works_and_syncs_served_ipv4(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    address = _address(db_session, pool, "198.51.100.12")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)

    assignment = network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    assert assignment.subscription_id == subscription.id
    assert assignment.subscriber_id == subscriber.id
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "198.51.100.12"


def test_create_ipv6_without_subscription_is_not_changed_by_this_guard(db_session):
    from app.models.network import IPv6Address

    pool = IpPool(
        name=f"Unbound v6 {uuid.uuid4().hex[:8]}",
        ip_version=IPVersion.ipv6,
        cidr="2001:db8:77::/48",
    )
    db_session.add(pool)
    db_session.flush()
    v6 = IPv6Address(address="2001:db8:77::10", pool_id=pool.id)
    db_session.add(v6)
    db_session.flush()
    subscriber = _subscriber(db_session)

    assignment = network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscriber_id=subscriber.id,
            ip_version=IPVersion.ipv6,
            ipv6_address_id=v6.id,
            prefix_length=64,
        ),
    )

    assert assignment.subscription_id is None


# --- admin form / service ---------------------------------------------------


def test_assign_ipv4_address_refuses_missing_subscription_without_writes(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)

    with pytest.raises(
        network_service.IPAssignmentError, match="subscription is required"
    ):
        ip_service.assign_ipv4_address(
            db_session,
            pool_id=str(pool.id),
            ip_address="198.51.100.20",
            subscriber_id=str(subscriber.id),
        )

    # The refusal happens before the IPv4Address row is created/committed.
    assert (
        db_session.query(IPv4Address)
        .filter(IPv4Address.address == "198.51.100.20")
        .count()
        == 0
    )


def test_assign_ipv4_address_refuses_blank_subscription(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)

    with pytest.raises(
        network_service.IPAssignmentError, match="subscription is required"
    ):
        ip_service.assign_ipv4_address(
            db_session,
            pool_id=str(pool.id),
            ip_address="198.51.100.21",
            subscriber_id=str(subscriber.id),
            subscription_id="   ",
        )


def test_form_action_returns_error_and_preselects_single_active_subscription(
    db_session, monkeypatch
):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)
    monkeypatch.setattr(actions, "_log_ip_audit_event", lambda *a, **k: None)

    result = actions.assign_ipv4_address_from_form(
        object(),
        db_session,
        {
            "pool_id": str(pool.id),
            "ip_address": "198.51.100.22",
            "subscriber_id": str(subscriber.id),
            "subscription_id": "",
        },
    )

    assert result.success is False
    assert "subscription is required" in (result.error or "").lower()
    ctx = result.form_context
    assert ctx is not None
    # Visible default the operator submits; nothing was bound server-side.
    assert ctx["subscription_id"] == str(subscription.id)
    assert ctx["subscription_label"]
    assert (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == subscriber.id)
        .count()
        == 0
    )


def test_form_action_does_not_preselect_when_two_candidate_subscriptions(
    db_session, monkeypatch
):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)
    _subscription(db_session, subscriber)
    _subscription(db_session, subscriber, SubscriptionStatus.blocked)
    monkeypatch.setattr(actions, "_log_ip_audit_event", lambda *a, **k: None)

    result = actions.assign_ipv4_address_from_form(
        object(),
        db_session,
        {
            "pool_id": str(pool.id),
            "ip_address": "198.51.100.23",
            "subscriber_id": str(subscriber.id),
        },
    )

    assert result.success is False
    assert not result.form_context["subscription_id"]
    assert not result.form_context["subscription_label"]


def test_default_subscription_counts_active_and_blocked_only(db_session):
    subscriber = _subscriber(db_session)
    assert (
        ip_service.default_subscription_for_subscriber(db_session, str(subscriber.id))
        is None
    )
    _subscription(db_session, subscriber, SubscriptionStatus.canceled)
    _subscription(db_session, subscriber, SubscriptionStatus.stopped)
    assert (
        ip_service.default_subscription_for_subscriber(db_session, str(subscriber.id))
        is None
    )
    blocked = _subscription(db_session, subscriber, SubscriptionStatus.blocked)
    default = ip_service.default_subscription_for_subscriber(
        db_session, str(subscriber.id)
    )
    assert default is not None and default["id"] == str(blocked.id)
    assert (
        ip_service.default_subscription_for_subscriber(db_session, "not-a-uuid") is None
    )
    assert ip_service.default_subscription_for_subscriber(db_session, "") is None


def test_form_action_with_subscription_assigns(db_session, monkeypatch):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)
    monkeypatch.setattr(actions, "_log_ip_audit_event", lambda *a, **k: None)

    result = actions.assign_ipv4_address_from_form(
        object(),
        db_session,
        {
            "pool_id": str(pool.id),
            "ip_address": "198.51.100.24",
            "subscriber_id": str(subscriber.id),
            "subscription_id": str(subscription.id),
        },
    )

    assert result.success is True
    row = db_session.query(IPAssignment).filter_by(subscriber_id=subscriber.id).one()
    assert row.subscription_id == subscription.id


# --- reassign never clears an existing service link -------------------------


def test_reassign_without_subscription_is_refused_and_keeps_link(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)
    other = _subscriber(db_session)
    ip_service.assign_ipv4_address(
        db_session,
        pool_id=str(pool.id),
        ip_address="198.51.100.30",
        subscriber_id=str(subscriber.id),
        subscription_id=str(subscription.id),
    )

    with pytest.raises(
        network_service.IPAssignmentError, match="subscription is required"
    ):
        ip_service.assign_ipv4_address(
            db_session,
            pool_id=str(pool.id),
            ip_address="198.51.100.30",
            subscriber_id=str(other.id),
        )

    row = db_session.query(IPAssignment).one()
    assert row.subscription_id == subscription.id
    assert row.subscriber_id == subscriber.id


def test_update_payload_without_subscription_keeps_existing_link(db_session):
    """The reassign update must omit subscription_id unless one is given."""
    pool = _pool(db_session, "198.51.100.0/24")
    address = _address(db_session, pool, "198.51.100.31")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)
    assignment = network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    network_service.ip_assignments.update(
        db=db_session,
        assignment_id=str(assignment.id),
        payload=IPAssignmentUpdate.model_validate({"is_active": True}),
    )

    db_session.refresh(assignment)
    assert assignment.subscription_id == subscription.id


def test_reassign_with_subscription_moves_link(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    old = _subscriber(db_session)
    old_sub = _subscription(db_session, old)
    new = _subscriber(db_session)
    new_sub = _subscription(db_session, new)
    ip_service.assign_ipv4_address(
        db_session,
        pool_id=str(pool.id),
        ip_address="198.51.100.32",
        subscriber_id=str(old.id),
        subscription_id=str(old_sub.id),
    )

    result = ip_service.assign_ipv4_address(
        db_session,
        pool_id=str(pool.id),
        ip_address="198.51.100.32",
        subscriber_id=str(new.id),
        subscription_id=str(new_sub.id),
    )

    assert result["reassigned"] is True
    assert result["assignment"].subscription_id == new_sub.id


# --- bulk CSV -----------------------------------------------------------------


def test_bulk_csv_row_without_subscription_is_refused_others_proceed(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    good = _subscriber(db_session)
    good_sub = _subscription(db_session, good)
    bare = _subscriber(db_session)

    summary = actions.bulk_assign_ipv4(
        db_session,
        [
            {
                "ip_address": "198.51.100.40",
                "subscriber": str(good.id),
                "subscription": str(good_sub.id),
            },
            {"ip_address": "198.51.100.41", "subscriber": str(bare.id)},
        ],
    )

    assert summary["assigned"] == 1
    assert len(summary["errors"]) == 1
    assert "subscription is required" in summary["errors"][0]["error"].lower()
    assert (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscription_id.is_(None))
        .count()
        == 0
    )


# --- import wizard path -------------------------------------------------------


def test_import_assign_requires_subscription(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)

    with pytest.raises(
        network_service.IPAssignmentError, match="subscription is required"
    ):
        ip_service.assign_ipv4_for_import(
            db_session,
            pool_id=str(pool.id),
            ip_address="198.51.100.50",
            subscriber_id=str(subscriber.id),
        )
    assert db_session.query(IPAssignment).count() == 0


def test_import_assign_with_subscription_still_works(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)

    assignment = ip_service.assign_ipv4_for_import(
        db_session,
        pool_id=str(pool.id),
        ip_address="198.51.100.51",
        subscriber_id=str(subscriber.id),
        subscription_id=str(subscription.id),
    )

    assert assignment.subscription_id == subscription.id


# --- bound callers keep working ----------------------------------------------


def test_schema_still_rejects_ipv4_without_address():
    with pytest.raises(ValidationError):
        IPAssignmentCreate(ip_version=IPVersion.ipv4)


def test_form_template_marks_subscription_required():
    from pathlib import Path

    html = Path(
        "templates/admin/network/ip-management/ipv4_assignment_form.html"
    ).read_text()
    assert "Search subscription (optional)" not in html
    assert "device record" in html
    assert 'name="subscription_id" data-typeahead-hidden' in html


# --- PATCH cannot strip the link; import validates ownership --------------------


def test_update_explicit_null_subscription_is_refused_for_bound_ipv4(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    address = _address(db_session, pool, "198.51.100.60")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)
    assignment = network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    with pytest.raises(network_service.IPAssignmentError) as exc:
        network_service.ip_assignments.update(
            db=db_session,
            assignment_id=str(assignment.id),
            payload=IPAssignmentUpdate.model_validate({"subscription_id": None}),
        )

    assert (
        exc.value.code == "network.ip_assignment_lifecycle.subscription_clear_refused"
    )
    db_session.refresh(assignment)
    assert assignment.subscription_id == subscription.id


def test_existing_unbound_row_stays_patchable(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    address = _address(db_session, pool, "198.51.100.61")
    subscriber = _subscriber(db_session)
    row = IPAssignment(
        subscriber_id=subscriber.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()

    updated = network_service.ip_assignments.update(
        db=db_session,
        assignment_id=str(row.id),
        payload=IPAssignmentUpdate.model_validate(
            {"subscription_id": None, "gateway": "198.51.100.1"}
        ),
    )

    assert updated.subscription_id is None
    assert updated.gateway == "198.51.100.1"


def test_import_assign_refuses_subscription_owned_by_another_subscriber(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    importer_target = _subscriber(db_session)
    other = _subscriber(db_session)
    foreign_sub = _subscription(db_session, other)

    with pytest.raises(network_service.IPAssignmentError) as exc:
        ip_service.assign_ipv4_for_import(
            db_session,
            pool_id=str(pool.id),
            ip_address="198.51.100.62",
            subscriber_id=str(importer_target.id),
            subscription_id=str(foreign_sub.id),
        )
    assert (
        exc.value.code
        == "network.ip_assignment_lifecycle.subscription_account_mismatch"
    )
    assert db_session.query(IPAssignment).count() == 0


def test_update_null_ip_version_and_null_subscription_cannot_bypass_guard(db_session):
    pool = _pool(db_session, "198.51.100.0/24")
    address = _address(db_session, pool, "198.51.100.63")
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)
    assignment = network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    with pytest.raises(network_service.IPAssignmentError) as exc:
        network_service.ip_assignments.update(
            db=db_session,
            assignment_id=str(assignment.id),
            payload=IPAssignmentUpdate.model_validate(
                {"ip_version": None, "subscription_id": None}
            ),
        )

    assert (
        exc.value.code == "network.ip_assignment_lifecycle.subscription_clear_refused"
    )
    db_session.refresh(assignment)
    assert assignment.subscription_id == subscription.id
