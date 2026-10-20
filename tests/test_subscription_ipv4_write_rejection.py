"""Generic subscription update/PATCH cannot change the served IPv4."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.schemas.catalog import SubscriptionUpdate
from app.services import catalog as catalog_service


def test_update_rejects_ipv4_change_with_pointer_to_replace(db_session, subscription):
    subscription.ipv4_address = "192.0.2.10"
    db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(ipv4_address="192.0.2.99"),
        )

    assert excinfo.value.status_code == 409
    assert "Replace IPv4" in str(excinfo.value.detail)
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "192.0.2.10"


def test_update_rejects_clearing_the_served_ipv4(db_session, subscription):
    subscription.ipv4_address = "192.0.2.10"
    db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(ipv4_address=None),
        )

    assert excinfo.value.status_code == 409


def test_update_allows_unchanged_ipv4_and_other_fields(db_session, subscription):
    subscription.ipv4_address = "192.0.2.10"
    db_session.commit()

    updated = catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(ipv4_address="192.0.2.10", service_description="ok"),
    )

    assert updated.ipv4_address == "192.0.2.10"
    assert updated.service_description == "ok"


def test_update_with_ipam_ip_never_writes_the_served_column(db_session, subscription):
    from app.models.network import IPAssignment, IPv4Address, IPVersion

    address = IPv4Address(address="192.0.2.40")
    db_session.add(address)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
        )
    )
    db_session.commit()

    # Drift state: the served column says .30, IPAM says .40.
    subscription.ipv4_address = "192.0.2.30"
    db_session.commit()

    updated = catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(ipv4_address="192.0.2.40"),
    )
    assert updated.ipv4_address == "192.0.2.30"

    with pytest.raises(HTTPException):
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(ipv4_address="192.0.2.41"),
        )


def test_initial_projection_refuses_foreign_address_or_set_column(
    db_session, subscription
):
    from app.models.network import IPAssignment, IPv4Address, IPVersion
    from app.services.ip_assignment_lifecycle import (
        IPv4AssignmentLifecycleError,
        project_initial_served_ipv4,
    )

    address = IPv4Address(address="192.0.2.50")
    db_session.add(address)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
            is_primary=True,
        )
    )
    db_session.flush()

    with pytest.raises(IPv4AssignmentLifecycleError):
        project_initial_served_ipv4(
            db_session, subscription=subscription, address="192.0.2.51"
        )
    project_initial_served_ipv4(
        db_session, subscription=subscription, address="192.0.2.50"
    )
    assert subscription.ipv4_address == "192.0.2.50"
    with pytest.raises(IPv4AssignmentLifecycleError):
        project_initial_served_ipv4(
            db_session, subscription=subscription, address="192.0.2.50"
        )


def _assign(db_session, subscription, value, *, primary):
    from app.models.network import IPAssignment, IPv4Address, IPVersion

    address = IPv4Address(address=value)
    db_session.add(address)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
            is_primary=primary,
        )
    )
    db_session.flush()


def test_initial_projection_with_two_blocks_uses_the_primary(db_session, subscription):
    from app.services.ip_assignment_lifecycle import (
        IPv4AssignmentLifecycleError,
        project_initial_served_ipv4,
    )

    _assign(db_session, subscription, "192.0.2.60", primary=True)
    _assign(db_session, subscription, "192.0.2.61", primary=False)

    with pytest.raises(IPv4AssignmentLifecycleError):
        project_initial_served_ipv4(
            db_session, subscription=subscription, address="192.0.2.61"
        )
    project_initial_served_ipv4(
        db_session, subscription=subscription, address="192.0.2.60"
    )
    assert subscription.ipv4_address == "192.0.2.60"


def test_initial_projection_refuses_address_held_by_another_service(
    db_session, subscription
):
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.services.ip_assignment_lifecycle import (
        IPv4AssignmentLifecycleError,
        project_initial_served_ipv4,
    )

    _assign(db_session, subscription, "192.0.2.70", primary=True)
    other = Subscription(
        subscriber_id=subscription.subscriber_id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.stopped,
        ipv4_address="192.0.2.70/32",
    )
    db_session.add(other)
    db_session.flush()

    with pytest.raises(IPv4AssignmentLifecycleError) as excinfo:
        project_initial_served_ipv4(
            db_session, subscription=subscription, address="192.0.2.70"
        )
    assert "served_ipv4_held_elsewhere" in str(excinfo.value.code)
    assert not subscription.ipv4_address
