"""Inbound CRM webhook receiver: HMAC auth and ticket-event dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.crm_webhooks import router
from app.config import settings
from app.db import get_db
from app.models.subscriber import Subscriber

SECRET = "test-webhook-secret"


@contextmanager
def _with_secret(value: str):
    """Temporarily set the frozen settings' webhook secrets."""
    original = settings.crm_webhook_secret
    original_customer = settings.crm_customer_webhook_secret
    object.__setattr__(settings, "crm_webhook_secret", value)
    object.__setattr__(settings, "crm_customer_webhook_secret", value)
    try:
        yield
    finally:
        object.__setattr__(settings, "crm_webhook_secret", original)
        object.__setattr__(settings, "crm_customer_webhook_secret", original_customer)


def _client(db_session=None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    if db_session is not None:
        app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client, body: dict, event: str, signature: str | None):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature-256"] = signature
    return client.post("/api/v1/webhooks/crm", content=raw, headers=headers)


def test_valid_ticket_created_enqueues_sync():
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(_client(), body, "ticket.created", _sign(raw))
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["args"] == ["abc-123"]


def test_bad_signature_rejected():
    body = {"ticket_id": "abc-123"}
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(_client(), body, "ticket.created", "sha256=deadbeef")
    assert resp.status_code == 401
    enqueue.assert_not_called()


def test_missing_signature_rejected():
    with _with_secret(SECRET):
        resp = _post(_client(), {"ticket_id": "x"}, "ticket.created", None)
    assert resp.status_code == 401


def test_unconfigured_secret_fails_closed():
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with _with_secret(""):
        resp = _post(_client(), body, "ticket.created", _sign(raw))
    assert resp.status_code == 503


def test_unknown_event_acknowledged_without_enqueue():
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(_client(), body, "invoice.paid", _sign(raw))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    enqueue.assert_not_called()


def test_missing_ticket_id_ignored():
    body = {"title": "no id"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(_client(), body, "ticket.created", _sign(raw))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    enqueue.assert_not_called()


def _post_customer(client, body: dict, event: str = "customer.accepted"):
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "X-Webhook-Event": event,
        "Content-Type": "application/json",
        "X-Webhook-Signature-256": _sign(raw),
    }
    return client.post("/api/v1/webhooks/crm/customers", content=raw, headers=headers)


def test_customer_accepted_creates_subscriber(db_session):
    crm_person_id = str(uuid.uuid4())
    body = {
        "crm_person_id": crm_person_id,
        "crm_project_id": str(uuid.uuid4()),
        "crm_quote_id": str(uuid.uuid4()),
        "first_name": "Ada",
        "last_name": "Lovelace",
        "display_name": "Ada Lovelace",
        "email": f"ada-{uuid.uuid4().hex[:8]}@example.com",
        "phone": "+2348000000000",
        "city": "Lagos",
    }

    with _with_secret(SECRET):
        resp = _post_customer(_client(db_session), body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "created"
    subscriber = db_session.get(Subscriber, payload["subscriber_id"])
    assert subscriber is not None
    assert subscriber.first_name == "Ada"
    assert subscriber.status.value == "new"
    assert subscriber.metadata_["crm_person_id"] == crm_person_id
    assert subscriber.metadata_["source"] == "dotmac_omni"


def test_customer_accepted_is_idempotent_by_crm_person_id(db_session):
    crm_person_id = str(uuid.uuid4())
    body = {
        "crm_person_id": crm_person_id,
        "first_name": "Grace",
        "last_name": "Hopper",
        "email": f"grace-{uuid.uuid4().hex[:8]}@example.com",
    }
    client = _client(db_session)

    with _with_secret(SECRET):
        first = _post_customer(client, body)
        second = _post_customer(client, body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "existing"
    assert first.json()["subscriber_id"] == second.json()["subscriber_id"]
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.metadata_["crm_person_id"].as_string() == crm_person_id)
        .count()
        == 1
    )


def test_customer_accepted_requires_crm_person_id(db_session):
    with _with_secret(SECRET):
        resp = _post_customer(_client(db_session), {"email": "missing@example.com"})

    assert resp.status_code == 400
