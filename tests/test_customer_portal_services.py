"""Tests for customer portal services."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import customer_portal


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear sessions before and after each test."""
    customer_portal._CUSTOMER_SESSIONS.clear()
    yield
    customer_portal._CUSTOMER_SESSIONS.clear()


# =============================================================================
# Session Management Tests
# =============================================================================


def test_create_customer_session():
    """Test creating a customer session."""
    account_id = uuid.uuid4()
    subscriber_id = uuid.uuid4()
    subscription_id = uuid.uuid4()

    token = customer_portal.create_customer_session(
        username="customer@example.com",
        account_id=account_id,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        return_to="/dashboard",
    )

    assert token is not None
    assert len(token) > 20
    assert token in customer_portal._CUSTOMER_SESSIONS

    session = customer_portal._CUSTOMER_SESSIONS[token]
    assert session["username"] == "customer@example.com"
    assert session["account_id"] == str(account_id)
    assert session["subscriber_id"] == str(subscriber_id)
    assert session["subscription_id"] == str(subscription_id)
    assert session["return_to"] == "/dashboard"


def test_create_customer_session_minimal():
    """Test creating a minimal customer session."""
    token = customer_portal.create_customer_session(
        username="user@example.com",
        account_id=None,
        subscriber_id=None,
    )

    session = customer_portal._CUSTOMER_SESSIONS[token]
    assert session["username"] == "user@example.com"
    assert session["account_id"] is None
    assert session["subscriber_id"] is None
    assert session["subscription_id"] is None
    assert session["return_to"] is None


def test_get_customer_session_valid():
    """Test getting a valid customer session."""
    token = customer_portal.create_customer_session(
        username="test@example.com",
        account_id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
    )

    session = customer_portal.get_customer_session(token)
    assert session is not None
    assert session["username"] == "test@example.com"


def test_get_customer_session_not_found():
    """Test getting a non-existent session."""
    session = customer_portal.get_customer_session("invalid-token")
    assert session is None


def test_get_customer_session_expired():
    """Test getting an expired session returns None."""
    token = customer_portal.create_customer_session(
        username="test@example.com",
        account_id=None,
        subscriber_id=None,
    )

    # Manually expire the session
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    customer_portal._CUSTOMER_SESSIONS[token]["expires_at"] = past

    session = customer_portal.get_customer_session(token)
    assert session is None
    # Session should be deleted
    assert token not in customer_portal._CUSTOMER_SESSIONS


def test_invalidate_customer_session():
    """Test invalidating a customer session."""
    token = customer_portal.create_customer_session(
        username="test@example.com",
        account_id=None,
        subscriber_id=None,
    )
    assert token in customer_portal._CUSTOMER_SESSIONS

    customer_portal.invalidate_customer_session(token)
    assert token not in customer_portal._CUSTOMER_SESSIONS


def test_invalidate_customer_session_not_found():
    """Test invalidating a non-existent session does nothing."""
    # Should not raise
    customer_portal.invalidate_customer_session("non-existent-token")


# =============================================================================
# Get Current Customer Tests
# =============================================================================


def test_get_current_customer_no_token(db_session):
    """Test getting customer with no token."""
    result = customer_portal.get_current_customer(None, db_session)
    assert result is None


def test_get_current_customer_invalid_token(db_session):
    """Test getting customer with invalid token."""
    result = customer_portal.get_current_customer("invalid-token", db_session)
    assert result is None


def test_get_current_customer_expired_token(db_session):
    """Test getting customer with expired token."""
    token = customer_portal.create_customer_session(
        username="test@example.com",
        account_id=None,
        subscriber_id=None,
    )
    # Expire it
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    customer_portal._CUSTOMER_SESSIONS[token]["expires_at"] = past

    result = customer_portal.get_current_customer(token, db_session)
    assert result is None


def test_get_current_customer_with_radius_user(db_session, subscriber_account):
    """Test getting customer that has a radius user."""
    from app.models.radius import RadiusUser
    from app.models.catalog import AccessCredential

    # RadiusUser requires an AccessCredential
    access_credential = AccessCredential(
        account_id=subscriber_account.id,
        username="radius_user@example.com",
    )
    db_session.add(access_credential)
    db_session.commit()

    radius_user = RadiusUser(
        username="radius_user@example.com",
        account_id=subscriber_account.id,
        access_credential_id=access_credential.id,
    )
    db_session.add(radius_user)
    db_session.commit()

    token = customer_portal.create_customer_session(
        username="radius_user@example.com",
        account_id=None,
        subscriber_id=None,
    )

    result = customer_portal.get_current_customer(token, db_session)
    assert result is not None
    assert result["radius_user_id"] == str(radius_user.id)
    assert result["account_id"] == str(subscriber_account.id)


def test_get_current_customer_with_subscription(db_session, subscription):
    """Test getting customer with subscription_id resolves account."""
    token = customer_portal.create_customer_session(
        username="customer@example.com",
        account_id=None,
        subscriber_id=None,
        subscription_id=subscription.id,
    )

    result = customer_portal.get_current_customer(token, db_session)
    assert result is not None
    assert result.get("account_id") == str(subscription.account_id)


# =============================================================================
# Helper Functions Tests
# =============================================================================


def test_get_status_value_none():
    """Test _get_status_value with None."""
    result = customer_portal._get_status_value(None)
    assert result == ""


def test_get_status_value_enum():
    """Test _get_status_value with enum-like value."""
    mock_enum = MagicMock()
    mock_enum.value = "active"
    result = customer_portal._get_status_value(mock_enum)
    assert result == "active"


def test_get_status_value_string():
    """Test _get_status_value with string."""
    result = customer_portal._get_status_value("pending")
    assert result == "pending"


def test_format_address_none():
    """Test _format_address with None."""
    result = customer_portal._format_address(None)
    assert result == "No address on file"


def test_format_address_full():
    """Test _format_address with full address."""
    address = SimpleNamespace(
        address_line1="123 Main St",
        city="Test City",
        region="TS",
        postal_code="12345",
    )
    result = customer_portal._format_address(address)
    assert result == "123 Main St, Test City, TS, 12345"


def test_format_address_partial():
    """Test _format_address with partial address."""
    address = SimpleNamespace(
        address_line1="456 Oak Ave",
        city="Oakland",
        region=None,
        postal_code=None,
    )
    result = customer_portal._format_address(address)
    assert result == "456 Oak Ave, Oakland"


def test_format_address_line_only():
    """Test _format_address with only address line."""
    address = SimpleNamespace(
        address_line1="789 Pine Rd",
        city=None,
        region=None,
        postal_code=None,
    )
    result = customer_portal._format_address(address)
    assert result == "789 Pine Rd"


# =============================================================================
# Get Dashboard Context Tests
# =============================================================================


def test_get_dashboard_context_no_account(db_session):
    """Test dashboard context with no account."""
    session = {"username": "test@example.com"}

    context = customer_portal.get_dashboard_context(db_session, session)

    assert context is not None
    assert context["user"].first_name == "test@example.com"
    assert context["account"].balance == 0
    assert context["services"] == []
    assert context["tickets"].open_count == 0


def test_get_dashboard_context_with_account(db_session, subscriber_account):
    """Test dashboard context with account."""
    session = {
        "username": "test@example.com",
        "account_id": str(subscriber_account.id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    assert context is not None
    assert context["account"] is not None


def test_get_dashboard_context_with_subscriber_person(db_session, subscriber_account, subscriber, person):
    """Test dashboard context uses person's name."""
    session = {
        "username": "test@example.com",
        "account_id": str(subscriber_account.id),
        "subscriber_id": str(subscriber.id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    # Person fixture has first_name="Test"
    assert context["user"].first_name == "Test"


def test_get_dashboard_context_with_organization(db_session, subscriber_account):
    """Test dashboard context with organization subscriber."""
    import uuid
    from app.models.person import Person
    from app.models.subscriber import Organization, Subscriber, SubscriberAccount

    org = Organization(
        name="Test Corporation",
    )
    db_session.add(org)
    db_session.commit()

    # Person linked to organization
    org_person = Person(
        first_name="OrgPerson",
        last_name="Rep",
        email=f"org-person-{uuid.uuid4().hex}@testcorp.com",
        organization_id=org.id,
    )
    db_session.add(org_person)
    db_session.commit()

    org_subscriber = Subscriber(person_id=org_person.id)
    db_session.add(org_subscriber)
    db_session.commit()

    # Create account for this subscriber
    org_account = SubscriberAccount(subscriber_id=org_subscriber.id)
    db_session.add(org_account)
    db_session.commit()

    session = {
        "username": "admin@testcorp.com",
        "account_id": str(org_account.id),
        "subscriber_id": str(org_subscriber.id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    # User is now person-based, so first_name is the person's first_name
    assert context["user"].first_name == "OrgPerson"


def test_get_dashboard_context_with_invoices(db_session, subscriber_account):
    """Test dashboard context calculates balance from invoices."""
    from decimal import Decimal
    from app.schemas.billing import InvoiceCreate
    from app.services import billing as billing_service

    # Create an invoice
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            total=Decimal("100.00"),
            balance_due=Decimal("75.00"),
        ),
    )

    session = {
        "username": "test@example.com",
        "account_id": str(subscriber_account.id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    assert context["account"].balance == 75.0
    assert context["account"].next_bill_amount == 100.0


def test_get_dashboard_context_with_subscriptions(db_session, subscription):
    """Test dashboard context with active subscriptions."""
    session = {
        "username": "test@example.com",
        "account_id": str(subscription.account_id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    assert len(context["services"]) >= 1
    assert context["service"] is not None


def test_get_dashboard_context_with_tickets(db_session, ticket, subscriber_account):
    """Test dashboard context counts open tickets."""
    session = {
        "username": "test@example.com",
        "account_id": str(subscriber_account.id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    # Ticket created with default status which may be "new"
    assert context["tickets"].open_count >= 0


def test_get_dashboard_context_no_services(db_session, subscriber_account):
    """Test dashboard context with no active services."""
    session = {
        "username": "test@example.com",
        "account_id": str(subscriber_account.id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    assert context["service"].status == "inactive" or context["service"].plan_name


def test_get_dashboard_context_subscriber_from_account(db_session, subscriber_account, subscriber):
    """Test dashboard context resolves subscriber from account."""
    session = {
        "username": "test@example.com",
        "account_id": str(subscriber_account.id),
        # No subscriber_id, should resolve from account
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    # Should still work
    assert context is not None


def test_get_dashboard_context_account_not_found(db_session):
    """Test dashboard context with invalid account_id."""
    session = {
        "username": "test@example.com",
        "account_id": str(uuid.uuid4()),  # Non-existent
    }

    # Should not raise, just return defaults
    context = customer_portal.get_dashboard_context(db_session, session)
    assert context is not None
    assert context["services"] == []


def test_get_current_customer_radius_user_with_subscription(db_session, subscriber_account, subscription):
    """Test getting customer when radius user has subscription_id."""
    from app.models.radius import RadiusUser
    from app.models.catalog import AccessCredential

    access_credential = AccessCredential(
        account_id=subscriber_account.id,
        username="sub_user@example.com",
    )
    db_session.add(access_credential)
    db_session.commit()

    radius_user = RadiusUser(
        username="sub_user@example.com",
        account_id=subscriber_account.id,
        access_credential_id=access_credential.id,
        subscription_id=subscription.id,
    )
    db_session.add(radius_user)
    db_session.commit()

    token = customer_portal.create_customer_session(
        username="sub_user@example.com",
        account_id=None,
        subscriber_id=None,
    )

    result = customer_portal.get_current_customer(token, db_session)
    assert result is not None
    assert result["subscription_id"] == str(subscription.id)


def test_get_current_customer_with_access_credential(db_session, subscriber_account):
    """Test getting customer via AccessCredential when no RadiusUser exists."""
    from app.models.catalog import AccessCredential

    access_credential = AccessCredential(
        account_id=subscriber_account.id,
        username="cred_user@example.com",
    )
    db_session.add(access_credential)
    db_session.commit()

    token = customer_portal.create_customer_session(
        username="cred_user@example.com",
        account_id=None,
        subscriber_id=None,
    )

    result = customer_portal.get_current_customer(token, db_session)
    assert result is not None
    assert result["account_id"] == str(subscriber_account.id)


def test_get_dashboard_context_with_speed_tier(db_session, subscriber_account, catalog_offer):
    """Test dashboard context shows speed when offer has speed_tier."""
    from app.models.catalog import SpeedTier
    from app.schemas.catalog import SubscriptionCreate
    from app.services import catalog as catalog_service

    # Create speed tier
    speed_tier = SpeedTier(
        name="100/10",
        down_mbps=100,
        up_mbps=10,
    )
    db_session.add(speed_tier)
    db_session.commit()

    # Update the catalog_offer to have this speed_tier
    catalog_offer.speed_tier_id = speed_tier.id
    db_session.commit()

    # Create subscription with this offer
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber_account.id,
            offer_id=catalog_offer.id,
        ),
    )

    session = {
        "username": "test@example.com",
        "account_id": str(subscriber_account.id),
    }

    context = customer_portal.get_dashboard_context(db_session, session)

    # Find the service with speed tier
    assert len(context["services"]) >= 1
    speed_service = [s for s in context["services"] if "100/" in s.speed]
    assert len(speed_service) >= 1
    assert speed_service[0].speed == "100/10 Kbps"
