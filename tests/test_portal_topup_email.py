"""Top-up/payment email must be a real address, not the RADIUS username.

Regression: get_topup_page used customer['username'] (the PPPoE login, or an
impersonation token) as the Paystack email, so Paystack rejected the top-up for
every RADIUS customer whose username is not an email.
"""

import uuid

from app.services.customer_portal_flow_payments import _resolve_customer_email


def test_resolves_subscriber_email_not_username(db_session, subscriber):
    # The session username is the PPPoE/RADIUS login, never an email.
    customer = {"account_id": str(subscriber.id), "username": "105000050"}
    resolved = _resolve_customer_email(db_session, customer)
    assert resolved == subscriber.email
    assert "@" in resolved


def test_prefers_session_email_when_present(db_session, subscriber):
    customer = {
        "account_id": str(subscriber.id),
        "username": "105000050",
        "email": "session@example.com",
    }
    assert _resolve_customer_email(db_session, customer) == "session@example.com"


def test_never_returns_username_when_no_subscriber(db_session):
    # Unknown account -> empty, NOT the username (which Paystack would reject).
    customer = {"account_id": str(uuid.uuid4()), "username": "105000050"}
    assert _resolve_customer_email(db_session, customer) == ""
