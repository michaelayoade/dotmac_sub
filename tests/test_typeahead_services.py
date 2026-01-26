"""Tests for typeahead services."""

import uuid
from decimal import Decimal

import pytest

from app.models.billing import Invoice
from app.models.person import Person
from app.models.subscriber import AccountRole, Organization, Subscriber, SubscriberAccount
from app.models.catalog import CatalogOffer, Subscription, ServiceType, AccessType
from app.services import typeahead


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestAccountLabel:
    """Tests for _account_label helper."""

    def test_person_subscriber(self, db_session, subscriber_account):
        """Test label for person subscriber."""
        label = typeahead._account_label(subscriber_account)
        assert "Test" in label or "User" in label

    def test_organization_subscriber(self, db_session, person):
        """Test label for organization subscriber."""
        org = Organization(name="Test Corp")
        db_session.add(org)
        db_session.commit()

        # Person linked to organization
        org_person = Person(
            first_name="Org",
            last_name="Contact",
            email=f"org-contact-{uuid.uuid4().hex}@example.com",
            organization_id=org.id,
        )
        db_session.add(org_person)
        db_session.commit()

        sub = Subscriber(person_id=org_person.id)
        db_session.add(sub)
        db_session.commit()

        account = SubscriberAccount(subscriber_id=sub.id)
        db_session.add(account)
        db_session.commit()
        db_session.refresh(account)

        label = typeahead._account_label(account)
        # The label should contain the organization name when subscriber has an organization
        assert "Test Corp" in label

    def test_with_account_number(self, db_session, subscriber):
        """Test label includes account number."""
        account = SubscriberAccount(
            subscriber_id=subscriber.id,
            account_number="ACC-123",
        )
        db_session.add(account)
        db_session.commit()
        db_session.refresh(account)

        label = typeahead._account_label(account)
        assert "ACC-123" in label

    def test_no_subscriber(self, db_session):
        """Test label when subscriber is None."""
        # Create account without loading subscriber
        account = SubscriberAccount(subscriber_id=uuid.uuid4(), account_number=None)
        account.subscriber = None

        label = typeahead._account_label(account)
        assert label == "Account"


class TestSubscriberLabel:
    """Tests for _subscriber_label helper."""

    def test_person_subscriber_label(self, db_session, subscriber):
        """Test label for person subscriber."""
        db_session.refresh(subscriber)
        label = typeahead._subscriber_label(subscriber)
        assert "Test" in label

    def test_organization_subscriber_label(self, db_session):
        """Test label for organization subscriber."""
        org = Organization(name="ACME Inc")
        db_session.add(org)
        db_session.commit()

        # Person linked to organization
        org_person = Person(
            first_name="ACME",
            last_name="Rep",
            email=f"acme-rep-{uuid.uuid4().hex}@example.com",
            organization_id=org.id,
        )
        db_session.add(org_person)
        db_session.commit()

        sub = Subscriber(person_id=org_person.id)
        db_session.add(sub)
        db_session.commit()
        db_session.refresh(sub)

        label = typeahead._subscriber_label(sub)
        # Label should contain person name since subscriber is person-based
        assert "ACME" in label or "Rep" in label


class TestSubscriptionLabel:
    """Tests for _subscription_label helper."""

    def test_with_offer_and_account(self, db_session, subscription):
        """Test label with offer and account."""
        db_session.refresh(subscription)
        label = typeahead._subscription_label(subscription)
        assert subscription.offer.name in label


class TestContactLabel:
    """Tests for _contact_label helper."""

    def test_contact_label_with_account(self, db_session, subscriber_account):
        """Test contact label includes account."""
        # Create a person
        contact_person = Person(
            first_name="John",
            last_name="Doe",
            email=f"john-doe-{uuid.uuid4().hex}@example.com",
        )
        db_session.add(contact_person)
        db_session.commit()

        # Create an AccountRole linking the person to the account
        role = AccountRole(
            account_id=subscriber_account.id,
            person_id=contact_person.id,
        )
        db_session.add(role)
        db_session.commit()
        db_session.refresh(role)

        label = typeahead._contact_label(role)
        assert "John" in label
        assert "Doe" in label


class TestInvoiceLabel:
    """Tests for _invoice_label helper."""

    def test_invoice_label_with_number(self, db_session, subscriber_account):
        """Test invoice label with number."""
        invoice = Invoice(
            account_id=subscriber_account.id,
            invoice_number="INV-001",
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )
        db_session.add(invoice)
        db_session.commit()
        db_session.refresh(invoice)

        label = typeahead._invoice_label(invoice)
        assert "INV-001" in label


# =============================================================================
# Typeahead Search Tests
# =============================================================================


class TestAccountsTypeahead:
    """Tests for accounts typeahead."""

    def test_empty_query(self, db_session):
        """Test empty query returns empty list."""
        results = typeahead.accounts(db_session, "", 10)
        assert results == []

    def test_whitespace_query(self, db_session):
        """Test whitespace query returns empty list."""
        results = typeahead.accounts(db_session, "   ", 10)
        assert results == []

    def test_search_by_account_number(self, db_session, subscriber):
        """Test searching by account number."""
        account = SubscriberAccount(
            subscriber_id=subscriber.id,
            account_number="SEARCH-ACC-123",
        )
        db_session.add(account)
        db_session.commit()

        results = typeahead.accounts(db_session, "SEARCH-ACC", 10)
        assert len(results) >= 1
        assert any(r["id"] == account.id for r in results)

    def test_search_by_person_name(self, db_session, subscriber_account, person):
        """Test searching by person name."""
        results = typeahead.accounts(db_session, person.first_name, 10)
        assert len(results) >= 1

    def test_limit_results(self, db_session, subscriber):
        """Test result limiting."""
        for i in range(5):
            account = SubscriberAccount(
                subscriber_id=subscriber.id,
                account_number=f"LIMIT-{i}",
            )
            db_session.add(account)
        db_session.commit()

        results = typeahead.accounts(db_session, "LIMIT", 2)
        assert len(results) <= 2


class TestSubscribersTypeahead:
    """Tests for subscribers typeahead."""

    def test_empty_query(self, db_session):
        """Test empty query returns empty list."""
        results = typeahead.subscribers(db_session, "", 10)
        assert results == []

    def test_search_by_person_name(self, db_session, subscriber, person):
        """Test searching by person name."""
        results = typeahead.subscribers(db_session, person.first_name, 10)
        assert len(results) >= 1

    def test_search_by_person_name_with_org(self, db_session):
        """Test searching by person name with organization."""
        org = Organization(name="Unique Org Name XYZ")
        db_session.add(org)
        db_session.commit()

        # Person linked to organization
        org_person = Person(
            first_name="UniqueSearchName",
            last_name="Rep",
            email=f"unique-rep-{uuid.uuid4().hex}@example.com",
            organization_id=org.id,
        )
        db_session.add(org_person)
        db_session.commit()

        sub = Subscriber(person_id=org_person.id)
        db_session.add(sub)
        db_session.commit()

        results = typeahead.subscribers(db_session, "UniqueSearchName", 10)
        assert len(results) >= 1


class TestSubscriptionsTypeahead:
    """Tests for subscriptions typeahead."""

    def test_empty_query(self, db_session):
        """Test empty query returns empty list."""
        results = typeahead.subscriptions(db_session, "", 10)
        assert results == []

    def test_search_by_offer_name(self, db_session, subscription):
        """Test searching by offer name."""
        offer_name = subscription.offer.name
        results = typeahead.subscriptions(db_session, offer_name, 10)
        assert len(results) >= 1


class TestContactsTypeahead:
    """Tests for contacts typeahead."""

    def test_empty_query(self, db_session):
        """Test empty query returns empty list."""
        results = typeahead.contacts(db_session, "", 10)
        assert results == []

    def test_search_by_contact_name(self, db_session, subscriber_account):
        """Test searching by contact name.

        The contacts typeahead searches Person via Subscriber.person_id,
        so we need to update the subscriber's person name for the search.
        """
        db_session.refresh(subscriber_account)
        subscriber = subscriber_account.subscriber
        db_session.refresh(subscriber)

        # Update the subscriber's person name to something unique
        subscriber.person.first_name = "UniqueContactName"
        subscriber.person.last_name = "Smith"
        db_session.commit()

        # Create an AccountRole linking a person to the account
        contact_person = Person(
            first_name="Contact",
            last_name="Person",
            email=f"contact-person-{uuid.uuid4().hex}@example.com",
        )
        db_session.add(contact_person)
        db_session.commit()

        role = AccountRole(
            account_id=subscriber_account.id,
            person_id=contact_person.id,
        )
        db_session.add(role)
        db_session.commit()

        results = typeahead.contacts(db_session, "UniqueContactName", 10)
        assert len(results) >= 1


class TestInvoicesTypeahead:
    """Tests for invoices typeahead."""

    def test_empty_query(self, db_session):
        """Test empty query returns empty list."""
        results = typeahead.invoices(db_session, "", 10)
        assert results == []

    def test_search_by_invoice_number(self, db_session, subscriber_account):
        """Test searching by invoice number."""
        invoice = Invoice(
            account_id=subscriber_account.id,
            invoice_number="UNIQUE-INV-789",
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )
        db_session.add(invoice)
        db_session.commit()

        results = typeahead.invoices(db_session, "UNIQUE-INV", 10)
        assert len(results) >= 1


# =============================================================================
# Response Wrapper Tests
# =============================================================================


class TestResponseWrappers:
    """Tests for response wrapper functions."""

    def test_accounts_response(self, db_session):
        """Test accounts_response returns proper format."""
        response = typeahead.accounts_response(db_session, "", 10)
        assert "items" in response
        assert "count" in response
        assert "limit" in response
        assert "offset" in response

    def test_subscribers_response(self, db_session):
        """Test subscribers_response returns proper format."""
        response = typeahead.subscribers_response(db_session, "", 10)
        assert "items" in response

    def test_subscriptions_response(self, db_session):
        """Test subscriptions_response returns proper format."""
        response = typeahead.subscriptions_response(db_session, "", 10)
        assert "items" in response

    def test_contacts_response(self, db_session):
        """Test contacts_response returns proper format."""
        response = typeahead.contacts_response(db_session, "", 10)
        assert "items" in response

    def test_invoices_response(self, db_session):
        """Test invoices_response returns proper format."""
        response = typeahead.invoices_response(db_session, "", 10)
        assert "items" in response
