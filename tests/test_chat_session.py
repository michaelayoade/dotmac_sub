"""Live-chat broker + inbound chat webhook.

The sub never lets a client self-declare identity: the broker asserts the
authenticated principal to the native team inbox and returns only an opaque
visitor token. The legacy chat webhook is HMAC-gated and fans agent replies
out to FCM.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from unittest.mock import patch

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
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    with _chat_settings():
        result = chat_session.broker_customer_session(db_session, str(sub.id))

    conversation = db_session.query(InboxConversation).one()
    assert result["visitor_token"]
    assert result["session_id"] == str(conversation.id)
    assert result["conversation_id"] == str(conversation.id)
    assert result["ws_url"] == "/ws/inbox"
    assert result["api_base"] == "/widget"
    assert conversation.metadata_["surface"] == "customer"
    assert conversation.metadata_["subscriber_id"] == str(sub.id)


def test_customer_session_carries_owned_ticket_context(db_session):
    from app.models.support import Ticket
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    ticket = Ticket(title="Router down", subscriber_id=sub.id)
    db_session.add(ticket)
    db_session.commit()
    with _chat_settings():
        chat_session.broker_customer_session(
            db_session, str(sub.id), ticket_id=str(ticket.id)
        )
    meta = db_session.query(InboxConversation).one().metadata_
    assert meta["ticket_id"] == str(ticket.id)
    assert (
        db_session.query(InboxConversation).one().subject
        == "Chat about a support ticket"
    )
    assert "project_id" not in meta


def test_customer_session_carries_customer_account_ticket_context(db_session):
    from app.models.support import Ticket
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    ticket = Ticket(title="Account ticket", customer_account_id=sub.id)
    db_session.add(ticket)
    db_session.commit()

    with _chat_settings():
        chat_session.broker_customer_session(
            db_session, str(sub.id), ticket_id=str(ticket.id)
        )

    meta = db_session.query(InboxConversation).one().metadata_
    assert meta["ticket_id"] == str(ticket.id)


def test_customer_session_carries_owned_project_context(db_session):
    from app.models.project_mirror import ProjectMirror
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    db_session.add(
        ProjectMirror(
            crm_project_id="pj-9", subscriber_id=sub.id, name="Install", status="active"
        )
    )
    db_session.commit()
    with _chat_settings():
        chat_session.broker_customer_session(db_session, str(sub.id), project_id="pj-9")
    meta = db_session.query(InboxConversation).one().metadata_
    assert meta["project_id"] == "pj-9"
    assert (
        db_session.query(InboxConversation).one().subject
        == "Chat about an installation project"
    )


def test_customer_session_drops_unowned_ticket_context(db_session):
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    with _chat_settings():
        # A ticket id the caller does not own (also not a real row) is dropped.
        chat_session.broker_customer_session(
            db_session, str(sub.id), ticket_id="11111111-1111-1111-1111-111111111111"
        )
    meta = db_session.query(InboxConversation).one().metadata_
    assert "ticket_id" not in meta
    assert db_session.query(InboxConversation).one().subject == "Chat with customer"


def test_customer_session_does_not_require_crm_settings(db_session):
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    with _chat_settings(config_id="", base=""):
        chat_session.broker_customer_session(db_session, str(sub.id))


# ── reseller broker ──────────────────────────────────────────────────────────


def test_reseller_session_prefers_reseller_user_identity(db_session):
    from app.models.team_inbox import InboxConversation
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
    with _chat_settings():
        result = chat_session.broker_reseller_session(
            db_session, str(reseller.id), principal
        )

    conversation = db_session.query(InboxConversation).one()
    assert result["conversation_id"] == str(conversation.id)
    assert conversation.contact_address == "agent@acme.example"
    assert conversation.metadata_["reseller_name"] == "Acme Agent"
    assert conversation.metadata_["surface"] == "reseller_portal"
    assert conversation.metadata_["reseller_id"] == str(reseller.id)


def test_reseller_session_falls_back_to_org_contact(db_session):
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    reseller = Reseller(name="Beta ISP", contact_email="contact@beta.example")
    db_session.add(reseller)
    db_session.commit()

    # A subscriber-backed reseller login (no reseller_user row).
    principal = {"principal_type": "subscriber", "principal_id": "irrelevant"}
    with _chat_settings():
        chat_session.broker_reseller_session(db_session, str(reseller.id), principal)

    conversation = db_session.query(InboxConversation).one()
    assert conversation.contact_address == "contact@beta.example"
    assert conversation.metadata_["reseller_name"] == "Beta ISP"


def test_reseller_session_accepts_customer_account_ticket_context(db_session):
    from app.models.support import Ticket
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    reseller = Reseller(name="Gamma ISP", contact_email="contact@gamma.example")
    account = Subscriber(
        first_name="Gamma",
        last_name="Customer",
        email="gamma-customer@example.com",
        reseller=reseller,
    )
    db_session.add_all([reseller, account])
    db_session.commit()
    ticket = Ticket(title="Managed account ticket", customer_account_id=account.id)
    db_session.add(ticket)
    db_session.commit()

    principal = {"principal_type": "subscriber", "principal_id": "irrelevant"}
    with _chat_settings():
        chat_session.broker_reseller_session(
            db_session, str(reseller.id), principal, ticket_id=str(ticket.id)
        )

    meta = db_session.query(InboxConversation).one().metadata_
    assert meta["ticket_id"] == str(ticket.id)


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

    def _sub(email):
        s = Subscriber(first_name="R", last_name="U", display_name="R U", email=email)
        db_session.add(s)
        db_session.flush()
        return s

    sub_a, sub_b, sub_c = (
        _sub("ra@example.com"),
        _sub("rb@example.com"),
        _sub("rc@example.com"),
    )
    s1, s2 = sub_a.id, sub_b.id
    db_session.add_all(
        [
            ResellerUser(reseller_id=reseller.id, subscriber_id=s1, is_active=True),
            ResellerUser(reseller_id=reseller.id, subscriber_id=s2, is_active=True),
            # Excluded: inactive, and a subscriber-less (Layer-3) login.
            ResellerUser(
                reseller_id=reseller.id, subscriber_id=sub_c.id, is_active=False
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
