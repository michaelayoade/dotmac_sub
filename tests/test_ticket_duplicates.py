"""Duplicate-candidate matching for support tickets (ported from CRM).

Covers the scoring service (`find_duplicate_ticket_candidates`), the
`GET /support/tickets/duplicates` API lookup, and the admin web create path's
409-style duplicate guard with the `duplicate_override` contract.
"""

from __future__ import annotations

import pytest

from app.api import support as support_api
from app.models.subscriber import Subscriber
from app.models.support import TicketStatus
from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.services import ticket_validation
from app.services import web_support_tickets as web_service


def _create_ticket(db_session, **overrides):
    data = {
        "title": "Customer service issue",
        "description": "Needs review",
        "priority": "normal",
    }
    data.update(overrides)
    return support_service.tickets.create(db_session, TicketCreate(**data))


def _form_values(**overrides) -> dict:
    values = {
        "title": "Customer service issue",
        "description": "Needs review",
        "subscriber_id": None,
        "customer_account_id": None,
        "customer_person_id": None,
        "region": None,
        "technician_person_id": None,
        "ticket_manager_person_id": None,
        "site_coordinator_person_id": None,
        "service_team_id": None,
        "ticket_type": None,
        "priority": "normal",
        "channel": "web",
        "status": "open",
        "due_at": None,
        "tags": None,
        "related_outage_ticket_id": None,
        "assignee_person_ids": [],
    }
    values.update(overrides)
    return values


def test_unassigned_status_alone_does_not_flag_duplicate(db_session):
    _create_ticket(
        db_session,
        title="Karasana OLT 1 Port 1",
        description="Pon 1 customers affected",
        ticket_type="Cabinet Disconnection",
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="GPON-JABI-3-pon-8 & 4",
            description="jabi",
            ticket_type="Cabinet Disconnection",
        ),
    )

    assert result.matches == []


def test_unassigned_ticket_can_match_on_strong_issue_content(db_session):
    existing = _create_ticket(
        db_session,
        title="GPON JABI PON 8 and 4 outage",
        description="Customers on GPON JABI PON 8 and 4 are down",
        ticket_type="Cabinet Disconnection",
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="GPON JABI PON 8 and 4 outage",
            description="Customers on GPON JABI PON 8 and 4 are down",
            ticket_type="Cabinet Disconnection",
        ),
    )

    assert [match.ticket_id for match in result.matches] == [str(existing.id)]
    assert "unassigned active ticket has a similar issue" in result.matches[0].reasons


def test_different_base_stations_do_not_match_on_generic_cabinet_text(db_session):
    _create_ticket(
        db_session,
        title="Karasana OLT 1 Port 1",
        description="Cabinet link disconnection.",
        ticket_type="Cabinet Disconnection",
        metadata={"base_station_details": "Karasana OLT 1 (Port 1)"},
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="SPDC OLT Port 5",
            description="Cabinet link disconnection.",
            ticket_type="Cabinet Disconnection",
            base_station_details="SPDC OLT (PORT 5)",
        ),
    )

    assert result.matches == []


def test_same_base_station_is_duplicate_signal(db_session):
    existing = _create_ticket(
        db_session,
        title="SPDC OLT Port 5",
        description="Cabinet link disconnection.",
        ticket_type="Cabinet Disconnection",
        metadata={"base_station_details": "SPDC OLT (PORT 5)"},
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="SPDC OLT Port 5",
            description="Cabinet link disconnection.",
            ticket_type="Cabinet Disconnection",
            base_station_details="spdc olt port 5",
        ),
    )

    assert [match.ticket_id for match in result.matches] == [str(existing.id)]
    assert "same base station" in result.matches[0].reasons


def test_same_subscriber_open_ticket_is_flagged(db_session, subscriber):
    existing = _create_ticket(
        db_session,
        title="Router keeps rebooting",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        ticket_type="Router Troubleshooting",
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="Router keeps rebooting",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
    )

    assert [match.ticket_id for match in result.matches] == [str(existing.id)]
    match = result.matches[0]
    assert "same subscriber" in match.reasons
    assert match.score >= ticket_validation.DUPLICATE_WARNING_THRESHOLD
    assert result.has_likely_duplicate
    assert match.subscriber_label == "Test User"


def test_other_subscribers_ticket_is_not_flagged(db_session, subscriber):
    other = Subscriber(
        first_name="Other",
        last_name="Person",
        email=f"other-{subscriber.id.hex}@example.com",
    )
    db_session.add(other)
    db_session.commit()
    _create_ticket(
        db_session,
        title="No internet at premises",
        subscriber_id=other.id,
        customer_account_id=other.id,
        ticket_type="Router Troubleshooting",
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="Billing question about my invoice",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
    )

    assert result.matches == []


def test_closed_tickets_are_ignored(db_session, subscriber):
    ticket = _create_ticket(
        db_session,
        title="Router keeps rebooting",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        ticket_type="Router Troubleshooting",
    )
    ticket.status = TicketStatus.closed.value
    db_session.commit()

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="Router keeps rebooting",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            ticket_type="Router Troubleshooting",
        ),
    )

    assert result.matches == []


def test_api_duplicate_lookup_returns_serialized_matches(db_session, subscriber):
    existing = _create_ticket(
        db_session,
        title="Router keeps rebooting",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        ticket_type="Router Troubleshooting",
    )

    payload = support_api.ticket_duplicate_lookup(
        title="Router keeps rebooting",
        description=None,
        exclude_ticket_id=None,
        subscriber_id=str(subscriber.id),
        customer_account_id=str(subscriber.id),
        customer_person_id=None,
        lead_id=None,
        ticket_type="Router Troubleshooting",
        base_station_details=None,
        tags=None,
        region=None,
        db=db_session,
    )

    assert payload["has_warning"] is True
    assert payload["matches"][0]["ticket_id"] == str(existing.id)
    assert payload["matches"][0]["reference"] == existing.number
    assert payload["matches"][0]["url"] == (f"/admin/support/tickets/{existing.number}")


def test_api_duplicate_lookup_excludes_named_ticket(db_session, subscriber):
    existing = _create_ticket(
        db_session,
        title="Router keeps rebooting",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        ticket_type="Router Troubleshooting",
    )

    payload = support_api.ticket_duplicate_lookup(
        title="Router keeps rebooting",
        description=None,
        exclude_ticket_id=str(existing.id),
        subscriber_id=str(subscriber.id),
        customer_account_id=str(subscriber.id),
        customer_person_id=None,
        lead_id=None,
        ticket_type="Router Troubleshooting",
        base_station_details=None,
        tags=None,
        region=None,
        db=db_session,
    )

    assert payload["has_warning"] is False
    assert payload["matches"] == []


def test_web_create_blocks_duplicate_without_override(db_session, subscriber):
    _create_ticket(
        db_session,
        title="Router keeps rebooting",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        ticket_type="Router Troubleshooting",
    )

    with pytest.raises(web_service.DuplicateTicketWarningError) as exc:
        web_service.create_ticket_from_form(
            db_session,
            request=None,
            actor_id=None,
            attachments=[],
            **_form_values(
                title="Router keeps rebooting",
                subscriber_id=str(subscriber.id),
                customer_account_id=str(subscriber.id),
                ticket_type="Router Troubleshooting",
            ),
        )

    assert exc.value.result.has_warning


def test_web_create_override_records_duplicate_metadata(db_session, subscriber):
    existing = _create_ticket(
        db_session,
        title="Router keeps rebooting",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        ticket_type="Router Troubleshooting",
    )

    ticket = web_service.create_ticket_from_form(
        db_session,
        request=None,
        actor_id=None,
        attachments=[],
        duplicate_override=True,
        **_form_values(
            title="Router keeps rebooting",
            subscriber_id=str(subscriber.id),
            customer_account_id=str(subscriber.id),
            ticket_type="Router Troubleshooting",
        ),
    )

    assert ticket.id is not None
    metadata = ticket.metadata_ or {}
    assert metadata.get("duplicate_override") is True
    references = [
        item["ticket_id"] for item in metadata.get("possible_duplicate_tickets") or []
    ]
    assert str(existing.id) in references
