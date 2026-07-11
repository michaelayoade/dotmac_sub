"""Vendor project-stub relay emitter (Phase 3, risk #6): flag gating, type
filtering, payload shape, idempotency of the push outcome, and event-hook wiring.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.config import settings
from app.models.project import Project
from app.services import vendor_project_relay as relay
from app.services.events.handlers.vendor_project_relay import VendorProjectRelayHandler
from app.services.events.types import Event, EventType


@contextmanager
def _settings(**overrides):
    """Temporarily override frozen-dataclass settings fields (object.__setattr__)."""
    original = {k: getattr(settings, k) for k in overrides}
    for k, v in overrides.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in original.items():
            object.__setattr__(settings, k, v)


def _project(db_session, subscriber, *, project_type="fiber_optics_installation"):
    project = Project(
        name="Native install",
        project_type=project_type,
        status="open",
        customer_address="12 Fiber Rd, Abuja",
        region="Abuja",
        subscriber_id=subscriber.id,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def _resp(status_code=200):
    return SimpleNamespace(status_code=status_code, text="ok")


# ── flag gating ───────────────────────────────────────────────────────────────


def test_push_disabled_when_flag_off(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber)
    monkeypatch.setattr(relay, "relay_enabled", lambda db: False)
    with patch.object(relay, "get_crm_client") as client:
        assert relay.push_project_stub(db_session, str(project.id)) == "disabled"
        client.assert_not_called()


def test_push_relays_when_flag_on(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber)
    monkeypatch.setattr(relay, "relay_enabled", lambda db: True)
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id", lambda db, sid: "crm-sub-1"
    )
    fake = MagicMock()
    fake.post_signed_webhook.return_value = _resp(200)
    with _settings(crm_webhook_secret="testsecret"), patch.object(
        relay, "get_crm_client", return_value=fake
    ):
        assert relay.push_project_stub(db_session, str(project.id)) == "relayed"

    # Signs the exact serialized body with X-Selfcare-Signature (CRM's HMAC path).
    _, kwargs = fake.post_signed_webhook.call_args
    body = kwargs["body"]
    expected = "sha256=" + hmac.new(b"testsecret", body, hashlib.sha256).hexdigest()
    assert kwargs["signature"] == expected
    assert fake.post_signed_webhook.call_args[0][0] == relay.RELAY_WEBHOOK_PATH


# ── type filtering ────────────────────────────────────────────────────────────


def test_push_skips_non_vendor_type(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber, project_type="cross_connect")
    monkeypatch.setattr(relay, "relay_enabled", lambda db: True)
    with patch.object(relay, "get_crm_client") as client:
        assert (
            relay.push_project_stub(db_session, str(project.id))
            == "not_vendor_relevant"
        )
        client.assert_not_called()


def test_air_fiber_installation_is_vendor_relevant(db_session, subscriber):
    project = _project(db_session, subscriber, project_type="air_fiber_installation")
    assert relay.is_vendor_relevant(project) is True


def test_push_missing_project(db_session):
    assert relay.push_project_stub(db_session, str(uuid4())) == "missing"


def test_push_no_secret(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber)
    monkeypatch.setattr(relay, "relay_enabled", lambda db: True)
    with _settings(crm_webhook_secret=""):
        assert relay.push_project_stub(db_session, str(project.id)) == "no_secret"


# ── payload shape ─────────────────────────────────────────────────────────────


def test_payload_shape(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber)
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id", lambda db, sid: "crm-sub-9"
    )
    payload = relay.build_relay_payload(db_session, project)
    assert payload == {
        "id": str(project.id),
        "name": "Native install",
        "status": "open",
        "project_type": "fiber_optics_installation",
        "customer_address": "12 Fiber Rd, Abuja",
        "region": "Abuja",
        "subscriber_external_ref": "crm-sub-9",
        "source": "sub_relay",
    }
    # Round-trips as JSON (the exact body posted).
    assert json.loads(json.dumps(payload))["source"] == "sub_relay"


# ── idempotency of the push outcome ───────────────────────────────────────────


def test_push_is_idempotent(db_session, subscriber, monkeypatch):
    """Re-pushing the same project (id = sub UUID) is a stable, repeatable relay —
    the CRM receiver upserts on that id, so the emitter can fire on every update."""
    project = _project(db_session, subscriber)
    monkeypatch.setattr(relay, "relay_enabled", lambda db: True)
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id", lambda db, sid: None
    )
    fake = MagicMock()
    fake.post_signed_webhook.return_value = _resp(200)
    with _settings(crm_webhook_secret="s"), patch.object(
        relay, "get_crm_client", return_value=fake
    ):
        first = relay.push_project_stub(db_session, str(project.id))
        second = relay.push_project_stub(db_session, str(project.id))
    assert first == second == "relayed"
    # Same id relayed both times → CRM upsert keyed on it stays idempotent.
    bodies = [c.kwargs["body"] for c in fake.post_signed_webhook.call_args_list]
    assert json.loads(bodies[0])["id"] == json.loads(bodies[1])["id"] == str(project.id)


# ── event-hook wiring ─────────────────────────────────────────────────────────


def _event(name, project_id):
    return Event(
        event_type=EventType.custom,
        payload={"name": name, "project_id": str(project_id), "project_name": "x"},
    )


def _set_handler_flag(monkeypatch, value):
    monkeypatch.setattr(
        "app.services.events.handlers.vendor_project_relay.relay_enabled",
        lambda db: value,
    )


def test_handler_enqueues_for_vendor_project(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber)
    _set_handler_flag(monkeypatch, True)
    with _settings(crm_base_url="https://crm.example"), patch(
        "app.services.queue_adapter.enqueue_task"
    ) as enq:
        VendorProjectRelayHandler().handle(db_session, _event("project.created", project.id))
    assert enq.called
    assert enq.call_args.args[0].name == (
        "app.tasks.vendor_project_relay.relay_project_stub_to_crm"
    )
    assert enq.call_args.kwargs["args"] == [str(project.id)]


def test_handler_skips_when_flag_off(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber)
    _set_handler_flag(monkeypatch, False)
    with _settings(crm_base_url="https://crm.example"), patch(
        "app.services.queue_adapter.enqueue_task"
    ) as enq:
        VendorProjectRelayHandler().handle(db_session, _event("project.updated", project.id))
    enq.assert_not_called()


def test_handler_skips_non_vendor_project(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber, project_type="cross_connect")
    _set_handler_flag(monkeypatch, True)
    with _settings(crm_base_url="https://crm.example"), patch(
        "app.services.queue_adapter.enqueue_task"
    ) as enq:
        VendorProjectRelayHandler().handle(db_session, _event("project.created", project.id))
    enq.assert_not_called()


def test_handler_ignores_unrelated_events(db_session, subscriber, monkeypatch):
    project = _project(db_session, subscriber)
    _set_handler_flag(monkeypatch, True)
    with _settings(crm_base_url="https://crm.example"), patch(
        "app.services.queue_adapter.enqueue_task"
    ) as enq:
        # Wrong custom name.
        VendorProjectRelayHandler().handle(db_session, _event("ticket.created", project.id))
        # Wrong event type entirely.
        VendorProjectRelayHandler().handle(
            db_session,
            Event(event_type=EventType.subscriber_created, payload={"name": "project.created"}),
        )
    enq.assert_not_called()


def test_handler_registered_in_dispatcher():
    from app.services.events.dispatcher import get_dispatcher, reset_dispatcher

    reset_dispatcher()
    try:
        handlers = get_dispatcher()._handlers
        assert any(type(h).__name__ == "VendorProjectRelayHandler" for h in handlers)
    finally:
        reset_dispatcher()
