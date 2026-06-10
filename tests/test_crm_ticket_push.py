"""Outbound ticket/comment push (Sub → CRM): linking, echo-guards, retries."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.support import Ticket, TicketComment
from app.services import crm_ticket_push
from app.services.crm_ticket_push import (
    TicketNotLinkedError,
    push_comment,
    push_ticket,
)


def _local_ticket(db, subscriber, **overrides):
    ticket = Ticket(
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "No internet"),
        description=overrides.pop("description", "Down since morning"),
        status="open",
        priority="normal",
        number=overrides.pop("number", f"TKT-{uuid4().hex[:6]}"),
        **overrides,
    )
    db.add(ticket)
    db.commit()
    return ticket


def _crm_client(monkeypatch, **responses):
    client = MagicMock()
    client.create_ticket.return_value = responses.get(
        "ticket",
        {
            "id": str(uuid4()),
            "number": "21500",
            "created_at": "2026-06-10T01:00:00Z",
            "updated_at": "2026-06-10T01:00:00Z",
        },
    )
    client.create_ticket_comment.return_value = responses.get(
        "comment", {"id": str(uuid4())}
    )
    monkeypatch.setattr(crm_ticket_push, "get_crm_client", lambda: client)
    return client


def test_push_ticket_links_and_adopts_crm_number(monkeypatch, db_session, subscriber):
    ticket = _local_ticket(db_session, subscriber)
    client = _crm_client(monkeypatch)
    crm_sub_id = str(uuid4())
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda db, sid: crm_sub_id,
    )

    outcome = push_ticket(db_session, str(ticket.id))
    db_session.refresh(ticket)

    assert outcome == "pushed"
    payload = client.create_ticket.call_args[0][0]
    assert payload["subscriber_id"] == crm_sub_id
    assert payload["title"] == "No internet"
    assert payload["metadata_"]["sub_ticket_id"] == str(ticket.id)
    assert ticket.metadata_["crm_ticket_id"]
    assert ticket.metadata_["sync_source"] == "crm"
    assert ticket.number == "21500"


def test_push_ticket_skips_already_linked(monkeypatch, db_session, subscriber):
    ticket = _local_ticket(db_session, subscriber)
    ticket.metadata_ = {"crm_ticket_id": str(uuid4())}
    db_session.commit()
    client = _crm_client(monkeypatch)

    assert push_ticket(db_session, str(ticket.id)) == "already_linked"
    client.create_ticket.assert_not_called()


def test_push_ticket_unresolved_subscriber(monkeypatch, db_session, subscriber):
    ticket = _local_ticket(db_session, subscriber)
    client = _crm_client(monkeypatch)
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda db, sid: None,
    )

    assert push_ticket(db_session, str(ticket.id)) == "unresolved_subscriber"
    client.create_ticket.assert_not_called()


def test_push_comment_links_crm_id(monkeypatch, db_session, subscriber):
    ticket = _local_ticket(db_session, subscriber)
    crm_tid = str(uuid4())
    ticket.metadata_ = {"crm_ticket_id": crm_tid, "sync_source": "crm"}
    comment = TicketComment(
        ticket_id=ticket.id, body="Any update?", author_type="customer"
    )
    db_session.add(comment)
    db_session.commit()
    client = _crm_client(monkeypatch)

    outcome = push_comment(db_session, str(comment.id))
    db_session.refresh(comment)

    assert outcome == "pushed"
    payload = client.create_ticket_comment.call_args[0][0]
    assert payload["ticket_id"] == crm_tid
    assert payload["body"] == "Any update?"
    assert comment.metadata_["crm_comment_id"]


def test_push_comment_echo_guard_for_pulled_comments(
    monkeypatch, db_session, subscriber
):
    ticket = _local_ticket(db_session, subscriber)
    ticket.metadata_ = {"crm_ticket_id": str(uuid4())}
    comment = TicketComment(
        ticket_id=ticket.id,
        body="From CRM",
        author_type="system",
        metadata_={"sync_source": "crm", "crm_comment_id": str(uuid4())},
    )
    db_session.add(comment)
    db_session.commit()
    client = _crm_client(monkeypatch)

    assert push_comment(db_session, str(comment.id)) == "already_linked"
    client.create_ticket_comment.assert_not_called()


def test_push_comment_retries_until_ticket_linked(monkeypatch, db_session, subscriber):
    ticket = _local_ticket(db_session, subscriber)  # no CRM link yet
    comment = TicketComment(ticket_id=ticket.id, body="hello", author_type="customer")
    db_session.add(comment)
    db_session.commit()
    _crm_client(monkeypatch)

    with pytest.raises(TicketNotLinkedError):
        push_comment(db_session, str(comment.id))


def test_push_comment_skips_internal(monkeypatch, db_session, subscriber):
    ticket = _local_ticket(db_session, subscriber)
    ticket.metadata_ = {"crm_ticket_id": str(uuid4())}
    comment = TicketComment(
        ticket_id=ticket.id, body="staff note", is_internal=True, author_type="staff"
    )
    db_session.add(comment)
    db_session.commit()
    client = _crm_client(monkeypatch)

    assert push_comment(db_session, str(comment.id)) == "internal_skipped"
    client.create_ticket_comment.assert_not_called()
