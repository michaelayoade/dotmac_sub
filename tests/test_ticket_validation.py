from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.services import ticket_validation


def _payload(**overrides) -> TicketCreate:
    data = {
        "title": "Customer service issue",
        "description": "Needs review",
        "priority": "normal",
    }
    data.update(overrides)
    return TicketCreate(**data)


def test_customer_created_ticket_requires_ticket_type(db_session, subscriber):
    with pytest.raises(HTTPException) as exc:
        ticket_validation.validate_ticket_creation(
            db_session,
            _payload(created_by_person_id=subscriber.id),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Ticket type is required."


def test_subscriber_required_ticket_type_requires_customer_link(db_session):
    with pytest.raises(HTTPException) as exc:
        ticket_validation.validate_ticket_creation(
            db_session,
            _payload(ticket_type="Router Replacement"),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Subscriber is required for the selected ticket type."


def test_base_station_required_ticket_type_requires_details(db_session):
    with pytest.raises(HTTPException) as exc:
        ticket_validation.validate_ticket_creation(
            db_session,
            _payload(ticket_type="BTS Outage"),
        )

    assert exc.value.status_code == 400
    assert (
        exc.value.detail
        == "Base station details are required for the selected ticket type."
    )


def test_base_station_required_ticket_type_accepts_metadata_details(db_session):
    ticket_validation.validate_ticket_creation(
        db_session,
        _payload(
            ticket_type="BTS Outage",
            metadata={"base_station_details": "Jabi mast sector 2"},
        ),
    )


def test_duplicate_open_ticket_context_allows_by_default(db_session, subscriber):
    first = support_service.tickets.create(
        db_session,
        _payload(
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
        actor_id=str(subscriber.id),
    )

    context = ticket_validation.build_pre_create_context(
        db_session,
        _payload(
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
    )
    second = support_service.tickets.create(
        db_session,
        _payload(
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
        actor_id=str(subscriber.id),
    )

    assert context["duplicate_ticket_id"] == str(first.id)
    assert second.id is not None


def test_duplicate_open_ticket_can_be_blocked_by_metadata_policy(
    db_session, subscriber
):
    support_service.tickets.create(
        db_session,
        _payload(
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
        actor_id=str(subscriber.id),
    )

    with pytest.raises(HTTPException) as exc:
        support_service.tickets.create(
            db_session,
            _payload(
                subscriber_id=subscriber.id,
                customer_account_id=subscriber.id,
                ticket_type="Router Troubleshooting",
                metadata={"duplicate_block": True},
            ),
            actor_id=str(subscriber.id),
        )

    assert exc.value.status_code == 409
    assert "Duplicate open ticket already exists" in str(exc.value.detail)


def test_duplicate_open_ticket_context_matches_customer_person_link(
    db_session, subscriber
):
    first = support_service.tickets.create(
        db_session,
        _payload(
            customer_person_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
        actor_id=str(subscriber.id),
    )

    context = ticket_validation.build_pre_create_context(
        db_session,
        _payload(
            customer_person_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
    )

    assert context["duplicate_ticket_id"] == str(first.id)


def test_duplicate_override_allows_repeated_ticket_type(db_session, subscriber):
    support_service.tickets.create(
        db_session,
        _payload(
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
        actor_id=str(subscriber.id),
    )

    ticket = support_service.tickets.create(
        db_session,
        _payload(
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
            metadata={"duplicate_override": True},
        ),
        actor_id=str(subscriber.id),
    )

    assert ticket.id is not None
