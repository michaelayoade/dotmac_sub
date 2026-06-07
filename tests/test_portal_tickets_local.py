"""Customer portal tickets are served by the internal (local) ticket module.

The portal previously depended on an external CRM (unconfigured here), so no
customer could open or view tickets. These flows now use the local
support.Tickets / TicketComments service so the portal works standalone.
"""

import uuid

import pytest

from app.models.support import Ticket
from app.services import crm_portal
from app.services import support as support_service


@pytest.fixture(autouse=True)
def _disable_ticket_numbering(monkeypatch):
    # The SQLite test schema has no `document_sequences` table; ticket numbering
    # is irrelevant to this behaviour, so skip it (number is nullable).
    monkeypatch.setattr(
        support_service.Tickets, "_resolve_ticket_number", lambda db: None
    )


def test_create_uses_local_ticket_module(db_session, subscriber):
    result = crm_portal.handle_ticket_create(
        db_session,
        customer={},
        subscriber_id=str(subscriber.id),
        title="Internet down",
        description="No light on the ONT",
        priority="high",
    )
    assert result["success"] is True, result
    ticket = result["ticket"]
    assert ticket["title"] == "Internet down"
    assert ticket["subscriber_id"] == str(subscriber.id)
    # persisted in the local support_tickets table
    assert db_session.get(Ticket, uuid.UUID(ticket["id"])) is not None


def test_list_and_detail_round_trip(db_session, subscriber):
    crm_portal.handle_ticket_create(
        db_session, {}, str(subscriber.id), "Slow speeds", "details", "normal"
    )
    ctx = crm_portal.tickets_list_context(
        None, db_session, {}, [str(subscriber.id)]
    )
    assert len(ctx["tickets"]) == 1
    tid = ctx["tickets"][0]["id"]

    detail = crm_portal.ticket_detail_context(
        None, db_session, {}, [str(subscriber.id)], tid
    )
    assert detail["ticket"] is not None
    assert detail["ticket"]["id"] == tid


def test_detail_enforces_ownership(db_session, subscriber):
    res = crm_portal.handle_ticket_create(
        db_session, {}, str(subscriber.id), "Mine", "d", "normal"
    )
    tid = res["ticket"]["id"]
    # A different subscriber must not be able to view it.
    other = str(uuid.uuid4())
    detail = crm_portal.ticket_detail_context(None, db_session, {}, [other], tid)
    assert detail["ticket"] is None
    assert detail.get("crm_error") is True


def test_comment_round_trip(db_session, subscriber):
    res = crm_portal.handle_ticket_create(
        db_session, {}, str(subscriber.id), "Need help", "d", "normal"
    )
    tid = res["ticket"]["id"]
    cres = crm_portal.handle_ticket_comment(
        db_session, {}, [str(subscriber.id)], tid, "Any update on this?"
    )
    assert cres["success"] is True, cres

    detail = crm_portal.ticket_detail_context(
        None, db_session, {}, [str(subscriber.id)], tid
    )
    assert any(c["body"] == "Any update on this?" for c in detail["comments"])
    # the customer's own comment shows as "You"
    assert detail["comments"][0]["author_name"] == "You"


def test_comment_rejected_for_non_owner(db_session, subscriber):
    res = crm_portal.handle_ticket_create(
        db_session, {}, str(subscriber.id), "Owned", "d", "normal"
    )
    tid = res["ticket"]["id"]
    cres = crm_portal.handle_ticket_comment(
        db_session, {}, [str(uuid.uuid4())], tid, "sneaky"
    )
    assert cres["success"] is False
