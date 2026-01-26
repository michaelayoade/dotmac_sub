"""Tests for contact wrapper service."""

import pytest

from app.services import contact as contact_service


class TestContactsList:
    """Tests for Contacts.list wrapper."""

    def test_lists_contacts_by_account(self, db_session, subscriber_account):
        """Test lists contacts for an account."""
        result = contact_service.contacts.list(
            db=db_session,
            account_id=str(subscriber_account.id),
        )
        # The wrapper delegates to subscriber_contacts.list, which may return empty
        assert isinstance(result, list)

    def test_lists_with_default_parameters(self, db_session):
        """Test lists contacts with default parameters."""
        result = contact_service.contacts.list(
            db=db_session,
            account_id=None,
        )
        assert isinstance(result, list)

    def test_accepts_custom_order_by(self, db_session, subscriber_account):
        """Test accepts custom order_by parameter."""
        result = contact_service.contacts.list(
            db=db_session,
            account_id=str(subscriber_account.id),
            order_by="created_at",
            order_dir="asc",
        )
        assert isinstance(result, list)

    def test_accepts_custom_pagination(self, db_session):
        """Test accepts custom pagination parameters."""
        result = contact_service.contacts.list(
            db=db_session,
            account_id=None,
            limit=10,
            offset=0,
        )
        assert isinstance(result, list)
