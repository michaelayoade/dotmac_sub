"""Live-chat broker + inbound chat webhook.

The sub never lets a client self-declare identity: the broker asserts the
authenticated principal to the CRM and returns only an opaque visitor token.
The chat webhook is HMAC-gated and fans agent replies out to FCM.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.crm_webhooks import receive_crm_chat_event
from app.config import settings
from app.models.subscriber import Reseller, ResellerUser, Subscriber

CHAT_SECRET = "test-chat-secret"


@contextmanager
def _chat_settings(*, enabled=True, config_id="cfg-1", base="https://crm.example"):
    saved = {
        k: getattr(settings, k)
        for k in (
            "chat_live_enabled",
            "crm_chat_config_id",
            "crm_base_url",
            "crm_chat_ws_url",
            "crm_webhook_secret",
        )
    }
    object.__setattr__(settings, "chat_live_enabled", enabled)
    object.__setattr__(settings, "crm_chat_config_id", config_id)
    object.__setattr__(settings, "crm_base_url", base)
    object.__setattr__(settings, "crm_chat_ws_url", "")
    object.__setattr__(settings, "crm_webhook_secret", CHAT_SECRET)
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(settings, k, v)


@contextmanager
def _fake_crm(return_value: dict):
    client = MagicMock()
    client.create_widget_session.return_value = return_value
    with patch("app.services.chat_session.get_crm_client", return_value=client):
        yield client


# ── customer broker ────────────────────────────────────────────────────────


def _make_subscriber(db_session):
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        display_name="Cust Omer",
        email="cust@example.com",
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def test_customer_session_disabled_returns_503(db_session):
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    with _chat_settings(enabled=False):
        with pytest.raises(HTTPException) as exc:
            chat_session.broker_customer_session(db_session, str(sub.id))
    assert exc.value.status_code == 503


def test_customer_session_happy_path(db_session):
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    crm_resp = {
        "session_id": "sess-1",
        "visitor_token": "vt-abc",
        "conversation_id": "conv-1",
    }
    with (
        _chat_settings(),
        _fake_crm(crm_resp) as client,
        patch(
            "app.services.chat_session.resolve_crm_subscriber_id",
            return_value="crm-sub-9",
        ),
    ):
        result = chat_session.broker_customer_session(db_session, str(sub.id))

    # Bundle the client needs to talk to the CRM directly.
    assert result["visitor_token"] == "vt-abc"
    assert result["session_id"] == "sess-1"
    assert result["conversation_id"] == "conv-1"
    assert result["ws_url"] == "wss://crm.example/ws/widget"
    assert result["api_base"] == "https://crm.example/widget"

    # Identity asserted server-side, never by the client.
    kwargs = client.create_widget_session.call_args.kwargs
    assert kwargs["email"] == "cust@example.com"
    assert kwargs["crm_subscriber_id"] == "crm-sub-9"
    assert kwargs["metadata"] == {"surface": "customer", "subscriber_id": str(sub.id)}
    assert kwargs["config_id"] == "cfg-1"


def test_customer_session_carries_owned_ticket_context(db_session):
    from app.models.support import Ticket
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    ticket = Ticket(title="Router down", subscriber_id=sub.id)
    db_session.add(ticket)
    db_session.commit()
    with (
        _chat_settings(),
        _fake_crm({"session_id": "s", "visitor_token": "v"}) as client,
        patch(
            "app.services.chat_session.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        chat_session.broker_customer_session(
            db_session, str(sub.id), ticket_id=str(ticket.id)
        )
    meta = client.create_widget_session.call_args.kwargs["metadata"]
    assert meta["ticket_id"] == str(ticket.id)
    assert meta["subject"] == "Chat about a support ticket"
    assert "project_id" not in meta


def test_customer_session_carries_owned_project_context(db_session):
    from app.models.project_mirror import ProjectMirror
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    db_session.add(
        ProjectMirror(
            crm_project_id="pj-9", subscriber_id=sub.id, name="Install", status="active"
        )
    )
    db_session.commit()
    with (
        _chat_settings(),
        _fake_crm({"session_id": "s", "visitor_token": "v"}) as client,
        patch(
            "app.services.chat_session.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        chat_session.broker_customer_session(db_session, str(sub.id), project_id="pj-9")
    meta = client.create_widget_session.call_args.kwargs["metadata"]
    assert meta["project_id"] == "pj-9"
    assert meta["subject"] == "Chat about an installation project"


def test_customer_session_drops_unowned_ticket_context(db_session):
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    with (
        _chat_settings(),
        _fake_crm({"session_id": "s", "visitor_token": "v"}) as client,
        patch(
            "app.services.chat_session.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        # A ticket id the caller does not own (also not a real row) is dropped.
        chat_session.broker_customer_session(
            db_session, str(sub.id), ticket_id="11111111-1111-1111-1111-111111111111"
        )
    meta = client.create_widget_session.call_args.kwargs["metadata"]
    assert "ticket_id" not in meta
    assert "subject" not in meta


def test_customer_session_crm_unavailable_returns_502(db_session):
    from app.services import chat_session
    from app.services.crm_client import CRMClientError

    sub = _make_subscriber(db_session)
    client = MagicMock()
    client.create_widget_session.side_effect = CRMClientError("circuit open")
    with (
        _chat_settings(),
        patch("app.services.chat_session.get_crm_client", return_value=client),
        patch("app.services.chat_session.resolve_crm_subscriber_id", return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            chat_session.broker_customer_session(db_session, str(sub.id))
    assert exc.value.status_code == 502


# ── reseller broker ──────────────────────────────────────────────────────────


def test_reseller_session_prefers_reseller_user_identity(db_session):
    from app.services import chat_session

    reseller = Reseller(name="Acme Networks", contact_email="owner@acme.example")
    db_session.add(reseller)
    db_session.commit()
    ru = ResellerUser(
        reseller_id=reseller.id,
        email="agent@acme.example",
        full_name="Acme Agent",
        is_active=True,
    )
    db_session.add(ru)
    db_session.commit()

    principal = {"principal_type": "reseller_user", "principal_id": str(ru.id)}
    crm_resp = {"session_id": "s", "visitor_token": "t", "conversation_id": None}
    with _chat_settings(), _fake_crm(crm_resp) as client:
        result = chat_session.broker_reseller_session(
            db_session, str(reseller.id), principal
        )

    assert result["conversation_id"] is None
    kwargs = client.create_widget_session.call_args.kwargs
    assert kwargs["email"] == "agent@acme.example"
    assert kwargs["name"] == "Acme Agent"
    assert kwargs["crm_subscriber_id"] is None
    assert kwargs["metadata"] == {
        "surface": "reseller_portal",
        "reseller_id": str(reseller.id),
    }


def test_reseller_session_falls_back_to_org_contact(db_session):
    from app.services import chat_session

    reseller = Reseller(name="Beta ISP", contact_email="contact@beta.example")
    db_session.add(reseller)
    db_session.commit()

    # A subscriber-backed reseller login (no reseller_user row).
    principal = {"principal_type": "subscriber", "principal_id": "irrelevant"}
    crm_resp = {"session_id": "s", "visitor_token": "t", "conversation_id": None}
    with _chat_settings(), _fake_crm(crm_resp) as client:
        chat_session.broker_reseller_session(db_session, str(reseller.id), principal)

    kwargs = client.create_widget_session.call_args.kwargs
    assert kwargs["email"] == "contact@beta.example"
    assert kwargs["name"] == "Beta ISP"


# ── inbound chat webhook → push ──────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, raw: bytes, headers: dict[str, str]):
        self._raw = raw
        self.headers = headers

    async def body(self) -> bytes:
        return self._raw


def _run(coro):
    box: dict[str, object] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_runner)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _sign(body: bytes, secret: str = CHAT_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_chat(db_session, body: dict, *, event="message.outbound", sig=None):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    if sig is not None:
        headers["X-Webhook-Signature-256"] = sig
    return _run(receive_crm_chat_event(_FakeRequest(raw, headers), db_session))


def test_chat_webhook_valid_signature_sends_push(db_session):
    body = {
        "subscriber_id": "sub-7",
        "conversation_id": "conv-7",
        "preview": "Hi, how can I help?",
    }
    raw = json.dumps(body).encode()
    with _chat_settings(), patch("app.services.push.send_push") as send:
        resp = _post_chat(db_session, body, sig=_sign(raw))
    assert resp["status"] == "ok"
    send.assert_called_once()
    assert send.call_args.args[1] == "sub-7"
    assert send.call_args.kwargs["data"]["conversation_id"] == "conv-7"
    assert send.call_args.kwargs["data"]["type"] == "chat_message"


def test_chat_webhook_reads_event_envelope(db_session):
    # The CRM delivers the data nested under "payload" (event envelope).
    body = {
        "event_type": "message.outbound",
        "payload": {
            "subscriber_id": "sub-9",
            "conversation_id": "conv-9",
            "preview": "Enveloped",
        },
        "context": {"subscriber_id": None},
    }
    raw = json.dumps(body).encode()
    with _chat_settings(), patch("app.services.push.send_push") as send:
        resp = _post_chat(db_session, body, sig=_sign(raw))
    assert resp["status"] == "ok"
    send.assert_called_once()
    assert send.call_args.args[1] == "sub-9"
    assert send.call_args.kwargs["data"]["conversation_id"] == "conv-9"


def test_chat_webhook_bad_signature_rejected(db_session):
    body = {"subscriber_id": "sub-7"}
    with _chat_settings(), patch("app.services.push.send_push") as send:
        with pytest.raises(HTTPException) as exc:
            _post_chat(db_session, body, sig="sha256=deadbeef")
    assert exc.value.status_code == 401
    send.assert_not_called()


def test_chat_webhook_unknown_event_ignored(db_session):
    body = {"subscriber_id": "sub-7"}
    raw = json.dumps(body).encode()
    with _chat_settings(), patch("app.services.push.send_push") as send:
        resp = _post_chat(
            db_session, body, event="conversation.snoozed", sig=_sign(raw)
        )
    assert resp["status"] == "ignored"
    send.assert_not_called()


def test_chat_webhook_reseller_wakes_portal_users(db_session):
    # A reseller chat carries reseller_id (no subscriber_id); the receiver wakes
    # every active reseller-portal user backed by a subscriber_id.
    reseller = Reseller(name="Acme Reseller")
    db_session.add(reseller)
    db_session.flush()
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    db_session.add_all(
        [
            ResellerUser(reseller_id=reseller.id, subscriber_id=s1, is_active=True),
            ResellerUser(reseller_id=reseller.id, subscriber_id=s2, is_active=True),
            # Excluded: inactive, and a subscriber-less (Layer-3) login.
            ResellerUser(
                reseller_id=reseller.id, subscriber_id=uuid.uuid4(), is_active=False
            ),
            ResellerUser(reseller_id=reseller.id, subscriber_id=None, is_active=True),
        ]
    )
    db_session.commit()

    body = {
        "reseller_id": str(reseller.id),
        "conversation_id": "conv-r",
        "preview": "Reseller reply",
    }
    raw = json.dumps(body).encode()
    with _chat_settings(), patch("app.services.push.send_push") as send:
        resp = _post_chat(db_session, body, sig=_sign(raw))
    assert resp["status"] == "ok"
    woken = {call.args[1] for call in send.call_args_list}
    assert woken == {str(s1), str(s2)}


def test_chat_webhook_reseller_no_devices_ignored(db_session):
    reseller = Reseller(name="Empty Reseller")
    db_session.add(reseller)
    db_session.commit()
    body = {"reseller_id": str(reseller.id), "conversation_id": "c", "preview": "x"}
    raw = json.dumps(body).encode()
    with _chat_settings(), patch("app.services.push.send_push") as send:
        resp = _post_chat(db_session, body, sig=_sign(raw))
    assert resp["status"] == "ignored"
    send.assert_not_called()


def test_chat_webhook_without_subscriber_is_acked_no_push(db_session):
    body = {"conversation_id": "conv-only"}  # reseller-originated / unmapped
    raw = json.dumps(body).encode()
    with _chat_settings(), patch("app.services.push.send_push") as send:
        resp = _post_chat(db_session, body, sig=_sign(raw))
    assert resp["status"] == "ignored"
    send.assert_not_called()
