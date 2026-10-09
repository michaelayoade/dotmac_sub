"""Top-up/payment email must be a real address, not the RADIUS username.

Regression: get_topup_page used customer['username'] (the PPPoE login, or an
impersonation token) as the Paystack email, so Paystack rejected the top-up for
every RADIUS customer whose username is not an email.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.api import me as me_api
from app.models.subscriber import Subscriber
from app.schemas.billing import TopupInitiateRequest
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


@pytest.mark.parametrize("username", ["", "105000050", "legacy@example.com"])
@pytest.mark.parametrize(
    ("provider", "charged"),
    [("paystack", False), ("paystack", True), ("flutterwave", False)],
)
def test_me_topup_initiate_returns_resolved_profile_email(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
    username: str,
    provider: str,
    charged: bool,
) -> None:
    _assert_topup_email_response(
        db_session, subscriber, monkeypatch, username, provider, charged
    )


def test_me_direct_transfer_without_email_keeps_nullable_response(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscriber.email = None
    db_session.flush()
    _assert_topup_email_response(
        db_session, subscriber, monkeypatch, "105000050", "direct_bank_transfer", False
    )


def _assert_topup_email_response(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
    username: str,
    provider: str,
    charged: bool,
) -> None:
    customer = {
        "account_id": str(subscriber.id),
        "subscriber_id": str(subscriber.id),
        "username": username,
    }
    monkeypatch.setattr(me_api, "_customer", lambda db, principal: customer)
    intent_id = uuid.uuid4()
    fingerprint = "x" * 64
    checkout_url = None if charged else "https://checkout.example.com/topup"

    def create_intent(
        db: Session,
        scoped_customer: dict[str, str],
        amount: Decimal,
        **kwargs: object,
    ) -> dict[str, object]:
        assert db is db_session
        assert scoped_customer == customer
        assert amount == Decimal("5000.00")
        assert kwargs["provider"] == provider
        assert kwargs["preview_fingerprint"] == fingerprint
        assert kwargs["idempotency_key"] == "topup-email-regression"
        return {
            "intent_id": str(intent_id),
            "provider_type": provider,
            "provider_public_key": "pk_test_topup",
            "reference": "topup-email-reference",
            "requested_amount": amount,
            "currency": "NGN",
            "preview_fingerprint": fingerprint,
            "charged": charged,
            "checkout_url": checkout_url,
        }

    monkeypatch.setattr(me_api.customer_payments, "create_topup_intent", create_intent)
    response = me_api.my_topup_initiate(
        TopupInitiateRequest(
            amount=Decimal("5000.00"),
            provider=provider,
            preview_fingerprint=fingerprint,
            idempotency_key="topup-email-regression",
        ),
        request=None,
        db=db_session,
        principal={"principal_type": "subscriber", "subscriber_id": str(subscriber.id)},
    )
    assert response.customer_email == subscriber.email
    assert response.intent_id == str(intent_id)
    assert response.provider_type == provider
    assert response.payment_reference == "topup-email-reference"
    assert response.amount == Decimal("5000.00")
    assert response.currency == "NGN"
    assert response.preview_fingerprint == fingerprint
    assert response.charged is charged
    assert response.checkout_url == checkout_url
