"""Inbound CRM webhook receiver: HMAC auth and ticket-event dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.crm_webhooks import router
from app.config import settings

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


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
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
