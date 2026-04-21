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
    def test_create_contact_stores_linked_channels_without_login(
        self, db_session, subscriber
    ):
        from app.models.subscriber import SubscriberContact
        from app.services import customer_portal_contacts

        form = customer_portal_contacts.normalize_contact_form(
            full_name=None,
            phone="08012345678",
            email="jane@example.com",
            whatsapp="08012345678",
            facebook="jane.contact",
            instagram=None,
            x_handle=None,
            telegram="@janecontact",
            linkedin=None,
            other_social=None,
            relationship=None,
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
        assert contact.full_name is None
        assert contact.whatsapp == "08012345678"
        assert contact.facebook == "jane.contact"
        assert contact.telegram == "@janecontact"
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
            phone="08000000000",
            email="old@example.com",
            contact_type="general",
        )
        db_session.add_all([other, contact])
        db_session.commit()

        form = customer_portal_contacts.normalize_contact_form(
            full_name=None,
            phone="08000000001",
            email="other-subscriber@example.com",
            whatsapp=None,
            facebook=None,
            instagram="new_handle",
            x_handle=None,
            telegram=None,
            linkedin=None,
            other_social="TikTok: new_handle",
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
        assert contact.full_name is None
        assert contact.instagram == "new_handle"
        assert contact.other_social == "TikTok: new_handle"
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

    def test_owner_session_can_update_contact_for_managed_account(
        self, db_session, subscriber
    ):
        from app.models.subscriber import Subscriber, SubscriberContact
        from app.services import customer_portal_contacts

        managed_account = Subscriber(
            first_name="Managed",
            last_name="Account",
            email="managed-account@example.com",
        )
        db_session.add(managed_account)
        db_session.flush()
        contact = SubscriberContact(
            subscriber_id=managed_account.id,
            phone="08000000000",
            email="managed-old@example.com",
            contact_type="general",
        )
        db_session.add(contact)
        db_session.commit()

        form = customer_portal_contacts.normalize_contact_form(
            full_name="Managed Contact",
            phone="08000000011",
            email="managed-new@example.com",
            whatsapp=None,
            facebook=None,
            instagram=None,
            x_handle=None,
            telegram=None,
            linkedin=None,
            other_social=None,
            relationship="manager",
            contact_type="billing",
            is_authorized=True,
            receives_notifications=True,
            is_billing_contact=False,
            notes="Portal owner updated this record",
        )

        warnings = customer_portal_contacts.update_contact(
            db_session,
            {"subscriber_id": str(subscriber.id), "account_id": str(managed_account.id)},
            str(contact.id),
            form,
        )

        db_session.refresh(contact)
        assert warnings == []
        assert contact.subscriber_id == managed_account.id
        assert contact.full_name == "Managed Contact"
        assert contact.phone == "08000000011"
        assert contact.email == "managed-new@example.com"
        assert contact.contact_type == "billing"
        assert contact.is_billing_contact is True
        assert contact.is_authorized is True

    def test_owner_session_creates_contact_for_selected_managed_account(
        self, db_session, subscriber
    ):
        from app.models.subscriber import Subscriber, SubscriberContact
        from app.services import customer_portal_contacts

        managed_account = Subscriber(
            first_name="Managed",
            last_name="Create",
            email="managed-create@example.com",
        )
        db_session.add(managed_account)
        db_session.commit()

        form = customer_portal_contacts.normalize_contact_form(
            full_name=None,
            phone="08000000111",
            email="managed-contact@example.com",
            whatsapp="08000000111",
            facebook=None,
            instagram=None,
            x_handle=None,
            telegram=None,
            linkedin=None,
            other_social=None,
            relationship=None,
            contact_type="general",
            is_authorized=False,
            receives_notifications=True,
            is_billing_contact=False,
            notes="Managed account contact",
        )

        warnings = customer_portal_contacts.create_contact(
            db_session,
            {"subscriber_id": str(subscriber.id), "account_id": str(managed_account.id)},
            form,
        )

        contact = db_session.query(SubscriberContact).one()
        assert warnings == []
        assert contact.subscriber_id == managed_account.id
        assert contact.phone == "08000000111"
        assert contact.email == "managed-contact@example.com"

    def test_managed_account_update_checks_duplicates_against_contact_owner(
        self, db_session, subscriber
    ):
        from app.models.subscriber import Subscriber, SubscriberContact
        from app.services import customer_portal_contacts

        managed_account = Subscriber(
            first_name="Managed",
            last_name="Dupes",
            email="managed-dupes@example.com",
            phone="08000000222",
        )
        db_session.add(managed_account)
        db_session.flush()
        contact = SubscriberContact(
            subscriber_id=managed_account.id,
            phone="08000000444",
            email="managed-old@example.com",
            contact_type="general",
        )
        subscriber.email = "owner-conflict@example.com"
        subscriber.phone = "08000000333"
        db_session.add(contact)
        db_session.commit()

        form = customer_portal_contacts.normalize_contact_form(
            full_name=None,
            phone="08000000333",
            email="owner-conflict@example.com",
            whatsapp=None,
            facebook=None,
            instagram=None,
            x_handle=None,
            telegram=None,
            linkedin=None,
            other_social=None,
            relationship=None,
            contact_type="general",
            is_authorized=False,
            receives_notifications=False,
            is_billing_contact=False,
            notes=None,
        )

        warnings = customer_portal_contacts.update_contact(
            db_session,
            {"subscriber_id": str(subscriber.id), "account_id": str(managed_account.id)},
            str(contact.id),
            form,
        )

        assert "This email is already used by another subscriber account." in warnings
        assert "This phone number is already used by another subscriber account." in warnings

    def test_owner_session_can_delete_contact_for_managed_account(
        self, db_session, subscriber
    ):
        from app.models.subscriber import Subscriber, SubscriberContact
        from app.services import customer_portal_contacts

        managed_account = Subscriber(
            first_name="Managed",
            last_name="Account",
            email="managed-delete@example.com",
        )
        db_session.add(managed_account)
        db_session.flush()
        contact = SubscriberContact(
            subscriber_id=managed_account.id,
            phone="08000000021",
            email="managed-delete-contact@example.com",
            contact_type="general",
        )
        db_session.add(contact)
        db_session.commit()

        customer_portal_contacts.delete_contact(
            db_session,
            {"subscriber_id": str(subscriber.id), "account_id": str(managed_account.id)},
            str(contact.id),
        )

        assert db_session.get(SubscriberContact, contact.id) is None
