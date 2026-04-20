"""Tests for contact wrapper service."""

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


class TestCustomerPortalContacts:
    def test_create_contact_stores_linked_contact_without_login(self, db_session, subscriber):
        from app.models.subscriber import SubscriberContact
        from app.services import customer_portal_contacts

        form = customer_portal_contacts.normalize_contact_form(
            full_name="Jane Contact",
            phone="08012345678",
            email="jane@example.com",
            relationship="spouse",
            contact_type="billing",
            is_authorized=True,
            receives_notifications=True,
            is_billing_contact=False,
            notes="Can confirm billing questions",
        )

        warnings = customer_portal_contacts.create_contact(
            db_session,
            {"subscriber_id": str(subscriber.id), "account_id": str(subscriber.id)},
            form,
        )

        contact = db_session.query(SubscriberContact).one()
        assert warnings == []
        assert contact.subscriber_id == subscriber.id
        assert contact.full_name == "Jane Contact"
        assert contact.contact_type == "billing"
        assert contact.is_billing_contact is True
        assert contact.is_authorized is True

    def test_update_contact_is_scoped_to_current_subscriber(self, db_session, subscriber):
        from app.models.subscriber import Subscriber, SubscriberContact
        from app.services import customer_portal_contacts

        other = Subscriber(
            first_name="Other",
            last_name="User",
            email="other-subscriber@example.com",
        )
        contact = SubscriberContact(
            subscriber_id=subscriber.id,
            full_name="Old Name",
            phone="08000000000",
            email="old@example.com",
            contact_type="general",
        )
        db_session.add_all([other, contact])
        db_session.commit()

        form = customer_portal_contacts.normalize_contact_form(
            full_name="New Name",
            phone="08000000001",
            email="other-subscriber@example.com",
            relationship="sibling",
            contact_type="technical",
            is_authorized=False,
            receives_notifications=False,
            is_billing_contact=False,
            notes=None,
        )

        warnings = customer_portal_contacts.update_contact(
            db_session,
            {"subscriber_id": str(subscriber.id), "account_id": str(subscriber.id)},
            str(contact.id),
            form,
        )

        db_session.refresh(contact)
        assert contact.full_name == "New Name"
        assert contact.contact_type == "technical"
        assert warnings == [
            "This email is already used by another subscriber account."
        ]

        try:
            customer_portal_contacts.update_contact(
                db_session,
                {"subscriber_id": str(other.id), "account_id": str(other.id)},
                str(contact.id),
                form,
            )
        except ValueError as exc:
            assert str(exc) == "Contact not found."
        else:
            raise AssertionError("Expected contact update to be scoped to owner")
