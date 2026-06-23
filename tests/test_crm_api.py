from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import crm as crm_routes
from app.config import settings
from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.usage import AccountingStatus, RadiusAccountingSession

TOKEN = "crm-test-token"


@pytest.fixture()
def crm_auth():
    original = settings.selfcare_api_token
    object.__setattr__(settings, "selfcare_api_token", TOKEN)
    try:
        yield
    finally:
        object.__setattr__(settings, "selfcare_api_token", original)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class _RouteResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def _call_route(func, *args, **kwargs) -> _RouteResponse:
    try:
        body = func(*args, **kwargs)
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, body)


def _request(query: dict[str, str] | str | None = None) -> Request:
    if isinstance(query, dict):
        query_string = urlencode(query).encode()
    elif query:
        query_string = str(query).encode()
    else:
        query_string = b""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/crm",
            "query_string": query_string,
            "headers": [],
        }
    )


def _subscriber(db_session, *, status=SubscriberStatus.active) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Lovelace",
        display_name="Ada Lovelace",
        email=f"ada-{uuid.uuid4().hex}@example.com",
        phone="08030000000",
        subscriber_number=f"SUB-{uuid.uuid4().hex[:8]}",
        account_number=f"ACC-{uuid.uuid4().hex[:8]}",
        city="Lekki",
        region="Lagos",
        status=status,
        account_start_date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _offer(db_session) -> CatalogOffer:
    offer = CatalogOffer(
        name="Fiber 50",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        speed_download_mbps=50,
        speed_upload_mbps=10,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=Decimal("15000.00"),
        )
    )
    db_session.flush()
    return offer


def _subscription(
    db_session, subscriber: Subscriber, offer: CatalogOffer
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _billing(db_session, subscriber: Subscriber, subscription: Subscription) -> None:
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        total=Decimal("15000.00"),
        balance_due=Decimal("5000.00"),
        billing_period_start=datetime(2026, 6, 1, tzinfo=UTC),
        billing_period_end=datetime(2026, 6, 30, tzinfo=UTC),
        issued_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Fiber 50 monthly service",
            amount=Decimal("15000.00"),
            unit_price=Decimal("15000.00"),
        )
    )
    db_session.add(
        Payment(
            account_id=subscriber.id,
            amount=Decimal("10000.00"),
            status=PaymentStatus.succeeded,
            paid_at=datetime(2026, 6, 15, tzinfo=UTC),
        )
    )
    db_session.flush()


def _session(db_session, subscription: Subscription) -> None:
    now = datetime.now(UTC)
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="SID-1",
            status_type=AccountingStatus.interim,
            session_start=now - timedelta(hours=2),
            last_update_at=now - timedelta(minutes=5),
            input_octets=123,
            output_octets=456,
        )
    )
    db_session.flush()


def test_ping_requires_bearer_token(crm_auth):
    missing = _call_route(crm_routes.require_crm_bearer)
    valid = _call_route(crm_routes.require_crm_bearer, f"Bearer {TOKEN}")

    assert missing.status_code == 401
    assert valid.status_code == 200
    assert crm_routes.ping() == {"status": "ok"}


def test_subscriber_list_embeds_services_billing_and_session_state(
    db_session, crm_auth
):
    subscriber = _subscriber(db_session)
    offer = _offer(db_session)
    subscription = _subscription(db_session, subscriber, offer)
    _billing(db_session, subscriber, subscription)
    _session(db_session, subscription)
    db_session.commit()

    response = _call_route(
        crm_routes.list_subscribers,
        _request({"include": "services,billing,session_state"}),
        db_session,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] >= 1
    row = body["data"][0]
    assert row["subscriber_number"] == subscriber.subscriber_number
    assert row["services"][0]["plan_name"] == "Fiber 50"
    assert row["services"][0]["speed"] == "50/10 Mbps"
    assert row["billing"]["balance"] == 5000.0
    assert row["billing"]["total_paid"] == 10000.0
    assert row["session_state"] == "online"
    assert row["last_seen"].endswith("Z")


def test_invalid_query_parameters_return_400_field_errors(crm_auth):
    response = _call_route(
        crm_routes.list_subscribers,
        _request({"include": "unknown"}),
        object(),
    )

    assert response.status_code == 400
    assert "include" in response.json()["detail"]["errors"]


def test_unknown_subscriber_returns_404(db_session, crm_auth):
    response = _call_route(
        crm_routes.subscriber_detail,
        str(uuid.uuid4()),
        db_session,
    )

    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "Subscriber not found."


def test_finance_and_session_endpoints(db_session, crm_auth):
    subscriber = _subscriber(db_session)
    offer = _offer(db_session)
    subscription = _subscription(db_session, subscriber, offer)
    _billing(db_session, subscriber, subscription)
    _session(db_session, subscription)
    db_session.commit()

    transactions = _call_route(
        crm_routes.finance_transactions,
        _request({"customer_id": str(subscriber.id), "date_from": "2026-06-01"}),
        db_session,
    )
    payments = _call_route(
        crm_routes.finance_payments,
        _request({"customer_id": str(subscriber.id), "date_to": "2026-06-30"}),
        db_session,
    )
    sessions = _call_route(
        crm_routes.subscriber_sessions,
        str(subscriber.id),
        db_session,
    )

    assert transactions.status_code == 200
    assert transactions.json()["data"][0]["service_id"] == str(subscription.id)
    assert payments.status_code == 200
    assert payments.json()["data"][0]["amount"] == 10000.0
    assert sessions.status_code == 200
    assert sessions.json()["data"][0]["bytes_downloaded"] == 456


def test_status_writeback_rejects_active_and_logs(db_session, crm_auth):
    subscriber = _subscriber(db_session, status=SubscriberStatus.active)
    db_session.commit()

    response = _call_route(
        crm_routes.update_subscriber_status,
        str(subscriber.id),
        payload={"status": "disabled", "reason": "retention_lost", "source": "crm"},
        x_crm_actor="crm-user-1",
        db=db_session,
    )

    assert response.status_code == 409
    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(subscriber.id))
        .one()
    )
    assert event.metadata_["result"] == "rejected_transition"
    assert event.actor_id == "crm-user-1"


def test_status_writeback_disables_suspended_subscriber(db_session, crm_auth):
    subscriber = _subscriber(db_session, status=SubscriberStatus.suspended)
    offer = _offer(db_session)
    subscription = _subscription(db_session, subscriber, offer)
    subscription.status = SubscriptionStatus.suspended
    db_session.commit()

    response = _call_route(
        crm_routes.update_subscriber_status,
        str(subscriber.id),
        payload={"status": "disabled", "reason": "retention_lost", "source": "crm"},
        x_crm_actor="crm-user-2",
        db=db_session,
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "disabled"
    db_session.refresh(subscriber)
    db_session.refresh(subscription)
    assert subscriber.status == SubscriberStatus.disabled
    assert subscription.status == SubscriptionStatus.disabled
