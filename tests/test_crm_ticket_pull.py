from __future__ import annotations

from uuid import uuid4

from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketComment
from app.services.crm_ticket_pull import pull_tickets


class FakeCrmClient:
    def __init__(self, tickets, subscribers, comments):
        self._tickets = tickets
        self._subscribers = subscribers
        self._comments = comments

    def list_tickets(self, **kwargs):
        limit = kwargs.get("limit", 200)
        offset = kwargs.get("offset", 0)
        return self._tickets[offset : offset + limit]

    def get_subscriber(self, subscriber_id):
        return self._subscribers[subscriber_id]

    def list_subscribers(self, **kwargs):
        external_system = kwargs.get("external_system")
        items = list(self._subscribers.values())
        if external_system:
            items = [
                item for item in items if item.get("external_system") == external_system
            ]
        return items

    def list_ticket_comments(self, ticket_id, **kwargs):
        return self._comments.get(ticket_id, [])


def test_pull_crm_ticket_preserves_number_and_maps_subscriber(db_session, subscriber):
    subscriber.splynx_customer_id = 201
    db_session.commit()
    crm_subscriber_id = str(uuid4())
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": crm_subscriber_id,
                "number": "20151",
                "title": "Router offline",
                "description": "Customer cannot browse",
                "status": "open",
                "priority": "high",
                "channel": "phone",
                "ticket_type": "support",
                "tags": ["noc"],
                "is_active": True,
            }
        ],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": "201",
            }
        },
        comments={
            crm_ticket_id: [
                {
                    "id": str(uuid4()),
                    "body": "Checked power.",
                    "is_internal": False,
                    "attachments": [],
                }
            ]
        },
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()

    assert result.created == 1
    ticket = db_session.query(Ticket).filter(Ticket.number == "20151").one()
    assert ticket.subscriber_id == subscriber.id
    assert ticket.title == "Router offline"
    assert ticket.metadata_["crm_ticket_id"] == crm_ticket_id
    assert db_session.query(TicketComment).filter_by(ticket_id=ticket.id).count() == 1


def test_pull_crm_ticket_is_idempotent_for_ticket_and_comments(db_session, subscriber):
    subscriber.splynx_customer_id = 201
    db_session.commit()
    crm_subscriber_id = str(uuid4())
    crm_ticket_id = str(uuid4())
    crm_comment_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": crm_subscriber_id,
                "number": "20151",
                "title": "Router offline",
                "status": "open",
                "priority": "high",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={
            crm_subscriber_id: {
                "id": crm_subscriber_id,
                "external_system": "splynx",
                "external_id": "201",
            }
        },
        comments={
            crm_ticket_id: [
                {
                    "id": crm_comment_id,
                    "body": "Checked power.",
                    "is_internal": False,
                    "attachments": [],
                }
            ]
        },
    )

    first = pull_tickets(db_session, client=client)
    second = pull_tickets(db_session, client=client)
    db_session.commit()

    assert first.created == 1
    assert second.updated == 1
    assert db_session.query(Ticket).filter(Ticket.number == "20151").count() == 1
    assert db_session.query(TicketComment).count() == 1


def test_pull_crm_ticket_skips_lead_only_tickets(db_session):
    client = FakeCrmClient(
        tickets=[
            {
                "id": str(uuid4()),
                "lead_id": str(uuid4()),
                "subscriber_id": None,
                "number": "20151",
                "title": "Lead ticket",
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)

    assert result.skipped_leads == 1
    assert db_session.query(Ticket).count() == 0


def test_pull_crm_ticket_maps_single_customer_id_pair_from_title(
    db_session, subscriber
):
    subscriber.splynx_customer_id = 24294
    db_session.commit()
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": str(uuid4()),
                "number": "21404",
                "title": "Gems Communications Ltd (100024294 - 24294)",
                "status": "open",
                "priority": "normal",
                "channel": "phone",
                "is_active": True,
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)
    db_session.commit()

    assert result.created == 1
    ticket = db_session.query(Ticket).filter(Ticket.number == "21404").one()
    assert ticket.subscriber_id == subscriber.id


def test_pull_crm_ticket_skips_ambiguous_customer_id_pairs(db_session, subscriber):
    subscriber.splynx_customer_id = 8270
    other_subscriber = Subscriber(
        first_name="Other",
        last_name="User",
        email=f"other-{uuid4()}@example.com",
        splynx_customer_id=10035,
    )
    db_session.add(other_subscriber)
    db_session.commit()
    crm_ticket_id = str(uuid4())
    client = FakeCrmClient(
        tickets=[
            {
                "id": crm_ticket_id,
                "subscriber_id": None,
                "number": "21217",
                "title": "Multiple customer link disconnection",
                "description": "(100008270 - 8270) and (100010035 - 10035)",
            }
        ],
        subscribers={},
        comments={},
    )

    result = pull_tickets(db_session, client=client)

    assert result.skipped_unmapped_subscribers == 1
    assert db_session.query(Ticket).count() == 0
