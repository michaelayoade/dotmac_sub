from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.subscriber import SubscriberStatus
from app.services.customer_context import (
    allowed_customer_account_ids,
    allowed_customer_subscriber_ids,
    customer_is_restricted,
    resolve_customer_account_ids,
    resolve_customer_context,
)
from app.services.customer_portal_context import (
    get_allowed_account_ids,
    is_subscriber_restricted,
    resolve_allowed_subscriber_ids,
    resolve_customer_account,
)
from tests.test_customer_plan_change_prepaid import _make_offer, _make_subscription


def test_customer_context_resolves_direct_session_ids(db_session, subscriber):
    customer = {
        "username": "portal-user",
        "subscriber_id": str(subscriber.id),
        "account_id": str(subscriber.id),
        "read_only": True,
    }

    context = resolve_customer_context(db_session, customer)

    assert context.username == "portal-user"
    assert context.subscriber_id == str(subscriber.id)
    assert context.account_id == str(subscriber.id)
    assert context.principal_id == str(subscriber.id)
    assert context.allowed_account_ids == (str(subscriber.id),)
    assert context.allowed_subscriber_ids == (str(subscriber.id),)
    assert context.read_only is True
    assert context.owns_account(subscriber.id) is True
    assert context.require_account_id() == str(subscriber.id)


def test_customer_context_falls_back_from_subscriber_to_active_subscription(
    db_session, subscriber
):
    offer = _make_offer(
        db_session,
        name="Context Plan",
        amount=Decimal("1000.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        offer,
        next_billing_at=datetime.now(UTC) + timedelta(days=30),
        start_at=datetime.now(UTC),
    )

    context = resolve_customer_context(
        db_session, {"subscriber_id": str(subscriber.id)}
    )

    assert context.account_id == str(subscriber.id)
    assert context.subscription_id == str(subscription.id)
    assert context.subscription == subscription
    assert context.owns_subscription(subscription) is True


def test_customer_context_rejects_foreign_subscription_scope(db_session, subscriber):
    other = type(subscriber)(
        first_name="Other",
        last_name="User",
        email="other-context@example.com",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    offer = _make_offer(
        db_session,
        name="Foreign Context Plan",
        amount=Decimal("1000.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        other,
        offer,
        next_billing_at=datetime.now(UTC) + timedelta(days=30),
        start_at=datetime.now(UTC),
    )

    context = resolve_customer_context(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
            "subscription_id": str(subscription.id),
        },
    )

    assert context.account_id == str(subscriber.id)
    assert context.subscription is None
    assert context.owns_subscription(subscription) is False


def test_customer_context_shared_restricted_status(db_session, subscriber):
    subscriber.status = SubscriberStatus.suspended
    db_session.commit()

    context = resolve_customer_context(
        db_session, {"subscriber_id": str(subscriber.id)}
    )

    assert context.is_restricted is True
    assert customer_is_restricted(db_session, subscriber.id) is True
    assert is_subscriber_restricted(db_session, subscriber.id) is True


def test_customer_portal_context_helpers_delegate_to_shared_scope(
    db_session, subscriber
):
    customer = {"subscriber_id": str(subscriber.id)}

    assert resolve_customer_account_ids(db_session, customer) == (
        str(subscriber.id),
        None,
    )
    assert resolve_customer_account(customer, db_session) == (str(subscriber.id), None)
    assert allowed_customer_account_ids(db_session, customer) == [str(subscriber.id)]
    assert get_allowed_account_ids(customer, db_session) == [str(subscriber.id)]
    assert allowed_customer_subscriber_ids(db_session, customer) == [str(subscriber.id)]
    assert resolve_allowed_subscriber_ids(customer, db_session) == [str(subscriber.id)]
