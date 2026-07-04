"""Inbound CRM webhook receiver: HMAC auth and ticket-event dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException

from app.api.crm_webhooks import receive_crm_customer, receive_crm_event, router
from app.config import settings
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.crm_webhook_delivery import CrmWebhookDelivery
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketComment

SECRET = "test-webhook-secret"


@contextmanager
def _with_secret(value: str):
    """Temporarily set the frozen settings' webhook secret."""
    original = settings.crm_webhook_secret
    object.__setattr__(settings, "crm_webhook_secret", value)
    try:
        yield
    finally:
        object.__setattr__(settings, "crm_webhook_secret", original)


class _FakeRequest:
    def __init__(self, raw: bytes, headers: dict[str, str]):
        self._raw = raw
        self.headers = headers

    async def body(self) -> bytes:
        return self._raw


class _RouteResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _run(coro):
    # Drive the async route on a fresh event loop in a dedicated thread so the
    # full-suite run is immune to a running event loop leaked by an earlier test
    # (asyncio.run() otherwise raises "cannot be called from a running event
    # loop"). The test SQLite engine uses check_same_thread=False + StaticPool,
    # so the shared session is safe to use from the worker thread.
    box: dict[str, object] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _post(
    db_session,
    body: dict,
    event: str,
    signature: str | None,
    *,
    delivery_id: str | None = None,
):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    headers["X-Webhook-Delivery-Id"] = delivery_id or str(uuid4())
    if signature is not None:
        headers["X-Webhook-Signature-256"] = signature
    try:
        payload = _run(receive_crm_event(_FakeRequest(raw, headers), db_session))
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, payload)


def _post_customer(db_session, body: dict, event: str = "customer.accepted"):
    raw = json.dumps(body).encode()
    headers = {
        "X-Webhook-Event": event,
        "X-Webhook-Signature-256": _sign(raw),
        "Content-Type": "application/json",
    }
    try:
        payload = _run(receive_crm_customer(_FakeRequest(raw, headers), db_session))
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, payload)


def _post_customer_raw(db_session, raw: bytes, headers: dict[str, str]):
    try:
        payload = _run(receive_crm_customer(_FakeRequest(raw, headers), db_session))
    except HTTPException as exc:
        return _RouteResponse(exc.status_code, {"detail": exc.detail})
    return _RouteResponse(200, payload)


def _http_app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


def _ticket_event_body(
    crm_ticket_id: str,
    *,
    event_type: str = "ticket.created",
    subscriber_id: str,
    number: str = "T-100",
    status: str = "open",
    updated_at: str = "2026-07-04T10:00:00Z",
) -> dict:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "occurred_at": updated_at,
        "context": {"subscriber_id": subscriber_id, "ticket_id": crm_ticket_id},
        "payload": {
            "id": crm_ticket_id,
            "number": number,
            "title": "Router offline",
            "description": "Customer cannot browse",
            "status": status,
            "priority": "normal",
            "channel": "web",
            "updated_at": updated_at,
            "created_at": "2026-07-04T09:00:00Z",
        },
    }


def test_valid_ticket_created_creates_local_ticket(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_subscriber_id = uuid4()
    subscriber.crm_subscriber_id = crm_subscriber_id
    db_session.commit()
    body = _ticket_event_body(
        crm_ticket_id,
        subscriber_id=str(crm_subscriber_id),
        number="WH-1",
    )
    raw = json.dumps(body).encode()
    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.created", _sign(raw))
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
    assert resp.json()["result"] == "created"
    ticket = db_session.query(Ticket).filter(Ticket.number == "WH-1").one()
    assert ticket.subscriber_id == subscriber.id
    assert ticket.metadata_["crm_ticket_id"] == crm_ticket_id


def test_bad_signature_rejected(db_session):
    body = {"event_type": "ticket.created", "payload": {"id": "abc-123"}}
    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.created", "sha256=deadbeef")
    assert resp.status_code == 401


def test_missing_signature_rejected(db_session):
    with _with_secret(SECRET):
        resp = _post(db_session, {"ticket_id": "x"}, "ticket.created", None)
    assert resp.status_code == 401


def test_unconfigured_secret_fails_closed(db_session):
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with _with_secret(""):
        resp = _post(db_session, body, "ticket.created", _sign(raw))
    assert resp.status_code == 503


def test_unknown_event_acknowledged_without_processing(db_session):
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with _with_secret(SECRET):
        resp = _post(db_session, body, "invoice.paid", _sign(raw))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert db_session.query(CrmWebhookDelivery).count() == 0


def test_missing_ticket_id_ignored(db_session):
    body = {"event_type": "ticket.created", "payload": {"title": "no id"}}
    raw = json.dumps(body).encode()
    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.created", _sign(raw))
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
    assert resp.json()["result"] == "ignored_missing_ticket_id"


def test_duplicate_delivery_id_ignored(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_subscriber_id = uuid4()
    subscriber.crm_subscriber_id = crm_subscriber_id
    db_session.commit()
    body = _ticket_event_body(
        crm_ticket_id,
        subscriber_id=str(crm_subscriber_id),
        number="WH-DUPE",
    )
    raw = json.dumps(body).encode()
    delivery_id = str(uuid4())

    with _with_secret(SECRET):
        first = _post(
            db_session,
            body,
            "ticket.created",
            _sign(raw),
            delivery_id=delivery_id,
        )
        second = _post(
            db_session,
            body,
            "ticket.created",
            _sign(raw),
            delivery_id=delivery_id,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert db_session.query(Ticket).filter(Ticket.number == "WH-DUPE").count() == 1
    assert db_session.query(CrmWebhookDelivery).count() == 1


def test_ticket_updated_updates_local_ticket(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_subscriber_id = uuid4()
    subscriber.crm_subscriber_id = crm_subscriber_id
    ticket = Ticket(
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        number="WH-UPD",
        title="Old title",
        metadata_={
            "sync_source": "crm",
            "crm_ticket_id": crm_ticket_id,
            "crm_updated_at": "2026-07-04T09:00:00Z",
        },
    )
    db_session.add(ticket)
    db_session.commit()
    body = _ticket_event_body(
        crm_ticket_id,
        event_type="ticket.updated",
        subscriber_id=str(crm_subscriber_id),
        number="WH-UPD",
        updated_at="2026-07-04T10:30:00Z",
    )
    body["payload"]["title"] = "New title"
    raw = json.dumps(body).encode()

    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.updated", _sign(raw))

    assert resp.status_code == 200
    assert resp.json()["result"] == "updated"
    db_session.refresh(ticket)
    assert ticket.title == "New title"
    assert ticket.metadata_["crm_updated_at"] == "2026-07-04T10:30:00Z"


def test_payload_subscriber_id_can_be_selfcare_external_id(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_subscriber_id = str(uuid4())
    body = _ticket_event_body(
        crm_ticket_id,
        subscriber_id=crm_subscriber_id,
        number="WH-SELFCARE",
    )
    body["payload"]["subscriber_id"] = str(subscriber.id)
    raw = json.dumps(body).encode()

    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.created", _sign(raw))

    assert resp.status_code == 200
    assert resp.json()["result"] == "created"
    ticket = db_session.query(Ticket).filter(Ticket.number == "WH-SELFCARE").one()
    assert ticket.subscriber_id == subscriber.id


def test_ticket_resolved_sets_resolved_status(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_subscriber_id = uuid4()
    subscriber.crm_subscriber_id = crm_subscriber_id
    db_session.commit()
    body = _ticket_event_body(
        crm_ticket_id,
        event_type="ticket.resolved",
        subscriber_id=str(crm_subscriber_id),
        number="WH-RES",
        status="resolved",
    )
    raw = json.dumps(body).encode()

    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.resolved", _sign(raw))

    assert resp.status_code == 200
    ticket = db_session.query(Ticket).filter(Ticket.number == "WH-RES").one()
    assert ticket.status == "resolved"


def test_ticket_escalated_sets_high_priority_when_missing(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_subscriber_id = uuid4()
    subscriber.crm_subscriber_id = crm_subscriber_id
    db_session.commit()
    body = _ticket_event_body(
        crm_ticket_id,
        event_type="ticket.escalated",
        subscriber_id=str(crm_subscriber_id),
        number="WH-ESC",
    )
    body["payload"].pop("priority")
    raw = json.dumps(body).encode()

    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.escalated", _sign(raw))

    assert resp.status_code == 200
    ticket = db_session.query(Ticket).filter(Ticket.number == "WH-ESC").one()
    assert ticket.priority == "high"


def test_ticket_comment_created_adds_comment(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_comment_id = str(uuid4())
    ticket = Ticket(
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        number="WH-COM",
        title="Needs help",
        metadata_={
            "sync_source": "crm",
            "crm_ticket_id": crm_ticket_id,
            "crm_updated_at": "2026-07-04T09:00:00Z",
        },
    )
    db_session.add(ticket)
    db_session.commit()
    body = {
        "event_id": str(uuid4()),
        "event_type": "ticket.comment_created",
        "occurred_at": "2026-07-04T11:00:00Z",
        "context": {"ticket_id": crm_ticket_id},
        "payload": {
            "id": crm_comment_id,
            "ticket_id": crm_ticket_id,
            "body": "Agent replied",
            "is_internal": False,
            "created_at": "2026-07-04T11:00:00Z",
        },
    }
    raw = json.dumps(body).encode()

    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.comment_created", _sign(raw))

    assert resp.status_code == 200
    assert resp.json()["result"] == "comment_created"
    comment = db_session.query(TicketComment).filter_by(ticket_id=ticket.id).one()
    assert comment.body == "Agent replied"
    assert comment.metadata_["crm_comment_id"] == crm_comment_id


def test_older_webhook_does_not_overwrite_newer_poll_state(db_session, subscriber):
    crm_ticket_id = str(uuid4())
    crm_subscriber_id = uuid4()
    subscriber.crm_subscriber_id = crm_subscriber_id
    ticket = Ticket(
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        number="WH-STALE",
        title="Current title",
        metadata_={
            "sync_source": "crm",
            "crm_ticket_id": crm_ticket_id,
            "crm_updated_at": "2026-07-04T12:00:00Z",
        },
    )
    db_session.add(ticket)
    db_session.commit()
    body = _ticket_event_body(
        crm_ticket_id,
        event_type="ticket.updated",
        subscriber_id=str(crm_subscriber_id),
        number="WH-STALE",
        updated_at="2026-07-04T11:00:00Z",
    )
    body["payload"]["title"] = "Stale title"
    raw = json.dumps(body).encode()

    with _with_secret(SECRET):
        resp = _post(db_session, body, "ticket.updated", _sign(raw))

    assert resp.status_code == 200
    assert resp.json()["result"] == "stale_ignored"
    db_session.refresh(ticket)
    assert ticket.title == "Current title"


def test_customer_accepted_creates_subscriber_and_returns_readable_id(db_session):
    body = {
        "crm_person_id": "19b9cf6c-3597-4d12-8950-3b41e88f66b2",
        "crm_project_id": "63b428bf-c663-466c-9ebe-21f7c8c62acd",
        "crm_quote_id": "71454e77-2e06-40c8-a4ec-9158cc3ca367",
        "crm_sales_order_id": "1daefc7e-d918-4c25-ada7-91589e80ba5d",
        "name": "Abdulkadir Aminu Umar",
        "email": "aminuumara@example.com",
        "phone": "+07011115972",
        "address": "12 Test Street",
        "status": "new",
        "metadata": {"subscriber_category": "residential"},
    }
    with _with_secret(SECRET):
        resp = _post_customer(db_session, body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["id"]
    assert data["subscriber_id"] == data["subscriber_number"]
    assert data["subscriber_number"].startswith("SUB-")
    assert data["account_number"].startswith("ACC-")

    subscriber = db_session.get(Subscriber, data["id"])
    assert subscriber is not None
    assert subscriber.email == "aminuumara@example.com"
    assert subscriber.metadata_["crm_project_id"] == body["crm_project_id"]


def test_customer_webhook_http_route_is_registered(db_session):
    app = _http_app(db_session)

    assert any(
        getattr(route, "path", None) == "/api/v1/webhooks/crm/customers"
        and "POST" in getattr(route, "methods", set())
        for route in app.routes
    )


def test_customer_accepted_retry_returns_existing_subscriber(db_session):
    body = {
        "crm_person_id": "ba6fe627-d9bc-4383-b018-11fb631b44b3",
        "crm_project_id": "9132439f-0a2b-4a3b-a1d2-8f47eb8b3674",
        "name": "Aliyu Hassan",
        "email": "hassanahlee8@example.com",
        "phone": "+09160483890",
        "status": "new",
    }
    with _with_secret(SECRET):
        first = _post_customer(db_session, body)
        second = _post_customer(db_session, body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert (
        db_session.query(Subscriber).filter(Subscriber.email == body["email"]).count()
        == 1
    )


def test_customer_webhook_audits_identity_overwrite(db_session):
    body = {
        "crm_person_id": "4cf4d62b-29a0-493e-8a0d-6409a18e8897",
        "name": "Original Customer",
        "email": "original.customer@example.com",
        "phone": "+09000000003",
        "status": "new",
    }
    changed = {
        **body,
        "name": "Changed Customer",
        "email": "changed.customer@example.com",
        "phone": "+09000000004",
        "address": {"city": "Lagos"},
        "status": "active",
    }

    with _with_secret(SECRET):
        first = _post_customer(db_session, body)
        second = _post_customer(db_session, changed)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "subscriber")
        .filter(AuditEvent.entity_id == first.json()["id"])
        .filter(AuditEvent.action == "crm_customer_identity_update")
        .one()
    )
    changes = event.metadata_["changes"]
    assert changes["display_name"] == {
        "old": "Original Customer",
        "new": "Changed Customer",
    }
    assert changes["email"] == {
        "old": "original.customer@example.com",
        "new": "changed.customer@example.com",
    }
    assert changes["phone"] == {"old": "+09000000003", "new": "+09000000004"}
    assert changes["city"] == {"old": None, "new": "Lagos"}
    assert event.metadata_["crm_person_id"] == body["crm_person_id"]


def test_customer_webhook_matches_existing_customer_by_normalized_phone(db_session):
    subscriber = Subscriber(
        first_name="Normalized",
        last_name="Customer",
        display_name="Normalized Customer",
        email="old.normalized@example.com",
        phone="08012345678",
    )
    db_session.add(subscriber)
    db_session.commit()

    body = {
        "name": "Normalized Customer",
        "email": "new.normalized@example.com",
        "phone": "+2348012345678",
        "status": "active",
    }

    with _with_secret(SECRET):
        response = _post_customer(db_session, body)

    assert response.status_code == 200
    assert response.json()["id"] == str(subscriber.id)
    assert db_session.query(Subscriber).count() == 1
    db_session.refresh(subscriber)
    assert subscriber.email == "new.normalized@example.com"


def test_shared_project_id_does_not_merge_distinct_customers(db_session):
    """A crm_project_id can span multiple customers, so it must NOT be used to
    dedupe — two distinct people on the same project stay distinct subscribers."""
    project_id = "5e9d2c11-7a44-4b0e-9a3c-2f1d6b8e4c77"
    first_body = {
        "crm_person_id": "11111111-1111-4111-8111-111111111111",
        "crm_project_id": project_id,
        "name": "Person One",
        "email": "person.one@example.com",
        "phone": "+09000000001",
        "status": "new",
    }
    second_body = {
        "crm_person_id": "22222222-2222-4222-8222-222222222222",
        "crm_project_id": project_id,
        "name": "Person Two",
        "email": "person.two@example.com",
        "phone": "+09000000002",
        "status": "new",
    }
    with _with_secret(SECRET):
        first = _post_customer(db_session, first_body)
        second = _post_customer(db_session, second_body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    # Person One must not have been overwritten with Person Two's email.
    person_one = db_session.get(Subscriber, first.json()["id"])
    assert person_one.email == "person.one@example.com"


def test_customer_webhook_rejects_bad_signature(db_session):
    raw = json.dumps({"name": "Bad Sig", "email": "bad@example.com"}).encode()
    with _with_secret(SECRET):
        resp = _post_customer_raw(
            db_session,
            raw,
            {
                "X-Webhook-Event": "customer.accepted",
                "X-Webhook-Signature-256": "sha256=bad",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401
