"""The live-chat broker.

The sub never lets a client self-declare identity: the broker asserts the
authenticated principal to the native team inbox and returns only an opaque
visitor token.

There is one authority and one destination. ADR 0006's temporary CRM broker,
the `comms.chat_session_authority` selector it was chosen by, and the inbound
`POST /webhooks/crm/chat` receiver that woke devices for CRM-held
conversations were all removed on 2026-08-30 with the CRM itself, so the tests
that exercised them are gone too. The structural half of that retirement --
that a second destination cannot come back -- is
`tests/architecture/test_single_chat_authority.py`.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.config import settings
from app.models.subscriber import Reseller, ResellerUser, Subscriber


@contextmanager
def _chat_settings(*, enabled=True, **_unused):
    saved = settings.chat_live_enabled
    object.__setattr__(settings, "chat_live_enabled", enabled)
    try:
        yield
    finally:
        object.__setattr__(settings, "chat_live_enabled", saved)


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
    from app.services import chat_session, team_inbox_widget

    sub = _make_subscriber(db_session)
    with _chat_settings(enabled=False):
        with pytest.raises(team_inbox_widget.TeamInboxWidgetError) as exc:
            chat_session.broker_customer_session(db_session, str(sub.id))
    assert exc.value.code == "communications.team_inbox_widget.disabled"


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
    from app.models.project import Project
    from app.models.team_inbox import InboxConversation
    from app.services import chat_session

    sub = _make_subscriber(db_session)
    project = Project(
        subscriber_id=sub.id,
        name="Install",
        status="active",
        project_type="fiber_optics_installation",
    )
    db_session.add(project)
    db_session.commit()
    with _chat_settings():
        chat_session.broker_customer_session(
            db_session, str(sub.id), project_id=str(project.id)
        )
    meta = db_session.query(InboxConversation).one().metadata_
    assert meta["project_id"] == str(project.id)
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


def test_customer_session_needs_no_external_configuration(db_session):
    """The native transport is self-contained.

    This test dates from when a broker could need a remote base URL and widget
    config id. It is kept because the property it asserts is now the design:
    opening a chat reads no integration installation and no external endpoint.
    """

    from app.services import chat_session

    sub = _make_subscriber(db_session)
    with _chat_settings():
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
