"""Inbound CRM webhook receiver: HMAC auth and ticket-event dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

from app.api.crm_webhooks import receive_crm_customer, receive_crm_event, router
from app.config import settings
from app.db import get_db
from app.models.subscriber import Subscriber

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


def _post(body: dict, event: str, signature: str | None):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature-256"] = signature
    try:
        payload = _run(receive_crm_event(_FakeRequest(raw, headers)))
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


def test_valid_ticket_created_enqueues_sync():
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw))
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
        resp = _post(body, "ticket.created", "sha256=deadbeef")
    assert resp.status_code == 401
    enqueue.assert_not_called()


def test_missing_signature_rejected():
    with _with_secret(SECRET):
        resp = _post({"ticket_id": "x"}, "ticket.created", None)
    assert resp.status_code == 401


def test_unconfigured_secret_fails_closed():
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with _with_secret(""):
        resp = _post(body, "ticket.created", _sign(raw))
    assert resp.status_code == 503


def test_unknown_event_acknowledged_without_enqueue():
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "invoice.paid", _sign(raw))
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
        resp = _post(body, "ticket.created", _sign(raw))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    enqueue.assert_not_called()


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
