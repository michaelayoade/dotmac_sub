"""Inbound CRM webhook receiver: HMAC auth and ticket-event dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException

from app.api.crm_webhooks import receive_crm_customer, receive_crm_event, router
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.subscriber import Gender, Subscriber, UserType
from app.schemas.subscriber import SubscriberRead
from app.services.crm_customers import upsert_customer_from_payload
from app.services.web_customer_details import build_customer_detail_snapshot
from tests.integration_platform_helpers import enable_crm_inbound

SECRET = "test-webhook-secret"


@contextmanager
def _with_secret(value: str):
    """Compatibility-free test scope; the secret lives in the installation."""
    yield


@pytest.fixture(autouse=True)
def _crm_inbound_installation(db_session, monkeypatch):
    enable_crm_inbound(db_session, monkeypatch, signing_secret=SECRET)


class _FakeRequest:
    def __init__(self, raw: bytes, headers: dict[str, str]):
        self._raw = raw
        self.headers = headers

    async def body(self) -> bytes:
        return self._raw

    async def json(self):
        return json.loads(self._raw)


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


def _post(body: dict, event: str, signature: str | None, db=None):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    if signature is not None:
        headers["X-Webhook-Signature-256"] = signature
    try:
        payload = _run(receive_crm_event(_FakeRequest(raw, headers), db))
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


def test_valid_ticket_created_enqueues_sync(monkeypatch, db_session):
    from app.services import control_registry

    monkeypatch.setenv("CRM_TICKET_PULL_ENABLED", "false")
    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": True}
    )
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["args"] == ["abc-123"]


def test_ticket_event_noop_when_pull_disabled(monkeypatch, db_session):
    """Flip kill switch: crm.ticket_pull off -> 200 ack, nothing enqueued."""
    from app.services import control_registry

    monkeypatch.setenv("CRM_TICKET_PULL_ENABLED", "true")
    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": False}
    )
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "reason": "ticket_observation_disabled",
        "event": "ticket.created",
    }
    enqueue.assert_not_called()


def test_ticket_event_noop_when_pull_setting_missing(monkeypatch, db_session):
    """No env, no DB row -> the control's on_missing default (off) applies,
    matching the scheduler beat entries' default."""
    monkeypatch.delenv("CRM_TICKET_PULL_ENABLED", raising=False)
    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "ticket_observation_disabled"
    enqueue.assert_not_called()


def test_ticket_branch_gated_by_canonical_control(monkeypatch, db_session):
    from app.services import control_registry

    monkeypatch.delenv("CRM_TICKET_PULL_ENABLED", raising=False)
    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": True}
    )

    body = {"ticket_id": "abc-123"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.json()["status"] == "queued"
    enqueue.assert_called_once()

    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": False}
    )
    body = {"ticket_id": "abc-456"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "ticket_observation_disabled"
    enqueue.assert_not_called()


def test_bad_signature_rejected(db_session):
    body = {"ticket_id": "abc-123"}
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", "sha256=deadbeef", db_session)
    assert resp.status_code == 401
    enqueue.assert_not_called()


def test_missing_signature_rejected(db_session):
    with _with_secret(SECRET):
        resp = _post({"ticket_id": "x"}, "ticket.created", None, db_session)
    assert resp.status_code == 401


def test_disabled_inbound_capability_fails_closed(db_session):
    from app.services.integrations import installations

    binding = installations.require_enabled_capability_binding(
        db_session,
        connector_key="dotmac.crm",
        capability_id="crm.events.receive.v1",
    )
    installations.retire_installation(
        db_session,
        installation_id=binding.installation_id,
        reason="test_disabled",
    )
    db_session.commit()
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with _with_secret(""):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
    assert resp.status_code == 503


def test_unknown_event_acknowledged_without_enqueue(db_session):
    body = {"ticket_id": "x"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "invoice.paid", _sign(raw), db_session)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    enqueue.assert_not_called()


def test_missing_ticket_id_ignored(monkeypatch, db_session):
    monkeypatch.setenv("CRM_TICKET_PULL_ENABLED", "true")
    body = {"title": "no id"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        resp = _post(body, "ticket.created", _sign(raw), db_session)
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
        "date_of_birth": "1990-05-14",
        "gender": "female",
        "status": "new",
        "metadata": {
            "subscriber_category": "residential",
            # Retained for CRM compatibility, but top-level values are authoritative.
            "date_of_birth": "1980-01-01",
            "gender": "male",
        },
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
    assert str(subscriber.date_of_birth) == "1990-05-14"
    assert subscriber.gender == Gender.female
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
        "date_of_birth": "1993-07-15",
        "gender": "female",
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
    assert changes["date_of_birth"] == {"old": None, "new": "1993-07-15"}
    assert changes["gender"] == {"old": "unknown", "new": "female"}
    assert event.metadata_["crm_person_id"] == body["crm_person_id"]


def test_customer_webhook_missing_identity_fields_preserve_existing_values(db_session):
    subscriber = Subscriber(
        first_name="Existing",
        last_name="Customer",
        email="existing.identity@example.com",
        date_of_birth=date(1988, 2, 3),
        gender=Gender.male,
        metadata_={"crm_person_id": "identity-preserve-1"},
    )
    db_session.add(subscriber)
    db_session.commit()

    body = {
        "crm_person_id": "identity-preserve-1",
        "name": "Existing Customer",
        "email": "existing.identity@example.com",
        "date_of_birth": "",
        "gender": None,
        "metadata": {"date_of_birth": "2000-01-01", "gender": "female"},
    }
    with _with_secret(SECRET):
        response = _post_customer(db_session, body)

    assert response.status_code == 200
    db_session.refresh(subscriber)
    assert str(subscriber.date_of_birth) == "1988-02-03"
    assert subscriber.gender == Gender.male


@pytest.mark.parametrize(
    "field,value", [("date_of_birth", "not-a-date"), ("gender", "invalid-gender")]
)
def test_customer_webhook_rejects_invalid_identity_values_without_overwrite(
    db_session, field, value
):
    # Asserted at the consequence owner: the route wraps this in inbox-receipt
    # bookkeeping whose fail path commits durably, which the outer-transaction
    # test harness cannot represent.
    subscriber = Subscriber(
        first_name="Safe",
        last_name="Customer",
        email="safe.identity@example.com",
        date_of_birth=date(1988, 2, 3),
        gender=Gender.male,
        metadata_={"crm_person_id": "identity-invalid-1"},
    )
    db_session.add(subscriber)
    db_session.commit()

    body = {
        "crm_person_id": "identity-invalid-1",
        "name": "Safe Customer",
        "email": "safe.identity@example.com",
        field: value,
    }
    with pytest.raises(HTTPException) as exc_info:
        upsert_customer_from_payload(db_session, body)

    assert exc_info.value.status_code == 422
    # Validation fails before any mutation, so the session holds no pending
    # writes; refresh proves the stored identity survived untouched.
    db_session.refresh(subscriber)
    assert str(subscriber.date_of_birth) == "1988-02-03"
    assert subscriber.gender == Gender.male


def test_customer_webhook_identity_replay_is_idempotent_and_shown_in_admin_detail(
    db_session,
):
    body = {
        "crm_person_id": "identity-replay-1",
        "name": "Replay Customer",
        "email": "replay.identity@example.com",
        "date_of_birth": "1994-04-05",
        "gender": "non_binary",
    }
    with _with_secret(SECRET):
        first = _post_customer(db_session, body)
        second = _post_customer(db_session, body)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert (
        db_session.query(Subscriber).filter(Subscriber.email == body["email"]).count()
        == 1
    )
    subscriber = db_session.get(Subscriber, first.json()["id"])
    assert subscriber is not None
    assert str(subscriber.date_of_birth) == "1994-04-05"
    assert subscriber.gender == Gender.non_binary

    subscriber.user_type = UserType.customer
    db_session.commit()
    detail = build_customer_detail_snapshot(db_session, str(subscriber.id))
    assert str(detail["customer"].date_of_birth) == "1994-04-05"
    assert detail["customer"].gender == Gender.non_binary

    api_projection = SubscriberRead.model_validate(subscriber).model_dump(mode="json")
    assert api_projection["date_of_birth"] == "1994-04-05"
    assert api_projection["gender"] == "non_binary"


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


# --- S4a: replay dedup on the previously un-deduped routes ---


def test_ticket_webhook_replay_is_deduped(monkeypatch, db_session):
    """A redelivery with the same delivery id must not enqueue a second pull."""
    from app.services import control_registry

    control_registry.update_canonical_feature_controls(
        db_session, payload={"crm.ticket_pull": True}
    )
    body = {"ticket_id": "t-1"}
    raw = json.dumps(body).encode()
    with (
        _with_secret(SECRET),
        patch("app.services.queue_adapter.enqueue_task") as enqueue,
    ):
        first = _post(body, "ticket.created", _sign(raw), db_session)
        replay = _post(body, "ticket.created", _sign(raw), db_session)
    assert first.status_code == 200
    assert replay.status_code == 200 and replay.json()["status"] == "queued"
    assert enqueue.call_count == 1
