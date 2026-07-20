"""Support-satisfaction (CSAT) rating on resolved/closed tickets."""

import pytest
from fastapi import HTTPException


def _ticket(db_session, subscriber, status="resolved"):
    from app.models.support import Ticket

    ticket = Ticket(subscriber_id=subscriber.id, title="Slow speeds", status=status)
    db_session.add(ticket)
    db_session.commit()
    return ticket


class TestTicketCsat:
    def test_set_satisfaction_stores_rating(self, db_session, subscriber):
        from app.services import support as support_service

        ticket = _ticket(db_session, subscriber, status="resolved")
        out = support_service.Tickets.set_satisfaction(
            db_session, ticket, rating=5, comment="Great help"
        )
        assert out.metadata_["csat"]["rating"] == 5
        assert out.metadata_["csat"]["comment"] == "Great help"
        # surfaced via the ORM property (what TicketRead.csat_rating reads)
        assert out.csat_rating == 5

    def test_empty_comment_stored_as_none(self, db_session, subscriber):
        from app.services import support as support_service

        ticket = _ticket(db_session, subscriber, status="closed")
        out = support_service.Tickets.set_satisfaction(
            db_session, ticket, rating=4, comment="   "
        )
        assert out.metadata_["csat"]["comment"] is None
        assert out.csat_rating == 4

    def test_rating_rejected_before_resolution(self, db_session, subscriber):
        from app.services import support as support_service

        ticket = _ticket(db_session, subscriber, status="open")
        with pytest.raises(HTTPException) as exc:
            support_service.Tickets.set_satisfaction(db_session, ticket, rating=4)
        assert exc.value.status_code == 409
        assert ticket.csat_rating is None

    def test_unrated_ticket_property_is_none(self, db_session, subscriber):
        ticket = _ticket(db_session, subscriber, status="resolved")
        assert ticket.csat_rating is None
