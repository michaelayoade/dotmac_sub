"""The subscriber detail billing email reads typed sources only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.subscriber import SubscriberContact
from app.services.customer_portal_contacts import account_billing_email
from app.services.web_subscriber_details import _build_subscriber_enrichment


def _contact(db_session, subscriber, **fields) -> SubscriberContact:
    contact = SubscriberContact(subscriber_id=subscriber.id, **fields)
    db_session.add(contact)
    db_session.flush()
    return contact


def test_falls_back_to_the_account_email(db_session, subscriber) -> None:
    assert account_billing_email(db_session, subscriber) == subscriber.email


def test_designated_billing_contact_wins(db_session, subscriber) -> None:
    _contact(
        db_session,
        subscriber,
        email="general@example.com",
        contact_type="general",
        is_billing_contact=False,
    )
    _contact(db_session, subscriber, contact_type="billing", is_billing_contact=True)
    _contact(
        db_session,
        subscriber,
        email="finance@example.com",
        contact_type="billing",
        is_billing_contact=True,
    )
    assert account_billing_email(db_session, subscriber) == "finance@example.com"


def test_oldest_billing_contact_is_stable(db_session, subscriber) -> None:
    now = datetime.now(UTC)
    _contact(
        db_session,
        subscriber,
        email="newer@example.com",
        is_billing_contact=True,
        created_at=now,
    )
    _contact(
        db_session,
        subscriber,
        email="older@example.com",
        is_billing_contact=True,
        created_at=now - timedelta(days=1),
    )
    assert account_billing_email(db_session, subscriber) == "older@example.com"


def test_enrichment_ignores_the_splynx_metadata_copy(db_session, subscriber) -> None:
    """A row the migration left (conflict/invalid) must not leak into display."""

    # Written behind the closed-key owner on purpose: this is the shape of a
    # legacy row the migration left for review, not a supported write.
    subscriber.metadata_ = {
        "splynx_status": "active",
        "splynx_billing_email": "legacy@example.com",
        "billing_email": "legacy-bare@example.com",
    }
    db_session.flush()

    enrichment = _build_subscriber_enrichment(db_session, subscriber)
    assert enrichment["billing_email"] == subscriber.email

    _contact(
        db_session, subscriber, email="finance@example.com", is_billing_contact=True
    )
    enrichment = _build_subscriber_enrichment(db_session, subscriber)
    assert enrichment["billing_email"] == "finance@example.com"
