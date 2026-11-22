"""Duplicate-candidate matching for support tickets (ported from CRM).

Covers the scoring service (`find_duplicate_ticket_candidates`), the
`GET /support/tickets/duplicates` API lookup, and the admin web create path's
409-style duplicate guard with the `duplicate_override` contract.
"""

from __future__ import annotations

import pytest

from app.api import support as support_api
from app.models.network import FdhCabinet
from app.models.network_monitoring import OutageIncident, OutageIncidentTicketLink
from app.models.subscriber import Subscriber
from app.models.support import TicketStatus
from app.schemas.support import TicketCreate
from app.services import infrastructure_ticket_selection, ticket_validation
from app.services import support as support_service
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
        ticket_type="Cabinet Link Fault",
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="GPON-JABI-3-pon-8 & 4",
            description="jabi",
            ticket_type="Cabinet Link Fault",
        ),
    )

    assert result.matches == []


def test_unassigned_ticket_can_match_on_strong_issue_content(db_session):
    existing = _create_ticket(
        db_session,
        title="GPON JABI PON 8 and 4 outage",
        description="Customers on GPON JABI PON 8 and 4 are down",
        ticket_type="Cabinet Link Fault",
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="GPON JABI PON 8 and 4 outage",
            description="Customers on GPON JABI PON 8 and 4 are down",
            ticket_type="Cabinet Link Fault",
        ),
    )

    assert [match.ticket_id for match in result.matches] == [str(existing.id)]
    assert "unassigned active ticket has a similar issue" in result.matches[0].reasons


def test_different_base_stations_do_not_match_on_generic_cabinet_text(db_session):
    _create_ticket(
        db_session,
        title="Karasana OLT 1 Port 1",
        description="Cabinet link disconnection.",
        ticket_type="Cabinet Link Fault",
        metadata={"base_station_details": "Karasana OLT 1 (Port 1)"},
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="SPDC OLT Port 5",
            description="Cabinet link disconnection.",
            ticket_type="Cabinet Link Fault",
            base_station_details="SPDC OLT (PORT 5)",
        ),
    )

    assert result.matches == []


def test_same_base_station_is_duplicate_signal(db_session):
    existing = _create_ticket(
        db_session,
        title="SPDC OLT Port 5",
        description="Cabinet link disconnection.",
        ticket_type="Cabinet Link Fault",
        metadata={"base_station_details": "SPDC OLT (PORT 5)"},
    )

    result = ticket_validation.find_duplicate_ticket_candidates(
        db_session,
        ticket_validation.TicketDuplicateInput(
            title="SPDC OLT Port 5",
            description="Cabinet link disconnection.",
            ticket_type="Cabinet Link Fault",
            base_station_details="spdc olt port 5",
        ),
    )

    assert [match.ticket_id for match in result.matches] == [str(existing.id)]
    assert "same base station" in result.matches[0].reasons


def test_manual_infrastructure_warning_requires_same_name_and_similar_issue(db_session):
    existing = _create_ticket(
        db_session,
        title="FDH 12 link down",
        description="Cabinet uplink has failed",
        metadata={"infrastructure_selection": {"mode": "manual", "name": "FDH-12"}},
    )
    existing.ticket_type = "Cabinet Disconnection"
    db_session.flush()
    duplicate_input = ticket_validation.InfrastructureTicketDuplicateInput(
        title="FDH 12 link down",
        description="Cabinet uplink has failed",
        ticket_type="Cabinet Disconnection",
        manual_name="fdh 12",
    )
    result = ticket_validation.find_infrastructure_ticket_duplicates(
        db_session, duplicate_input
    )
    assert [match.ticket_id for match in result.matches] == [str(existing.id)]
    assert (
        ticket_validation.find_infrastructure_ticket_duplicates(
            db_session,
            ticket_validation.InfrastructureTicketDuplicateInput(
                title=duplicate_input.title,
                description=duplicate_input.description,
                ticket_type=duplicate_input.ticket_type,
                manual_name="FDH 13",
            ),
        ).matches
        == []
    )
    assert (
        ticket_validation.find_infrastructure_ticket_duplicates(
            db_session,
            ticket_validation.InfrastructureTicketDuplicateInput(
                title="Power equipment inspection",
                description="Routine inspection of batteries",
                ticket_type=duplicate_input.ticket_type,
                manual_name="FDH 12",
            ),
        ).matches
        == []
    )


def test_asset_infrastructure_warning_uses_asset_identity(db_session):
    from uuid import uuid4

    cabinet = FdhCabinet(name=f"FDH {uuid4().hex[:8]}")
    db_session.add(cabinet)
    db_session.flush()
    asset_id = cabinet.id
    other_asset_id = uuid4()
    existing = _create_ticket(
        db_session,
        title="Cabinet uplink down",
        description="Cabinet uplink has failed",
    )
    existing.ticket_type = "Cabinet Disconnection"
    db_session.flush()
    incident = OutageIncident(status="resolved", fdh_cabinet_id=asset_id)
    db_session.add(incident)
    db_session.flush()
    db_session.add(
        OutageIncidentTicketLink(
            incident_id=incident.id,
            ticket_id=existing.id,
            role="infrastructure",
            source="operator",
        )
    )
    db_session.flush()

    def lookup(target_id):
        return ticket_validation.find_infrastructure_ticket_duplicates(
            db_session,
            ticket_validation.InfrastructureTicketDuplicateInput(
                title="Cabinet uplink down",
                description="Cabinet uplink has failed",
                ticket_type="Cabinet Disconnection",
                asset_kind=infrastructure_ticket_selection.InfrastructureKind.cabinet,
                asset_id=target_id,
            ),
        )

    assert [match.ticket_id for match in lookup(asset_id).matches] == [str(existing.id)]
    assert lookup(other_asset_id).matches == []
    incident.status = "open"
    db_session.flush()
    assert lookup(asset_id).matches == []  # canonical outage path reuses its ticket


def test_manual_infrastructure_create_warns_then_records_override(
    db_session, monkeypatch
):
    from uuid import uuid4

    existing = _create_ticket(
        db_session,
        title="FDH 12 uplink down",
        description="Cabinet uplink has failed",
        metadata={"infrastructure_selection": {"mode": "manual", "name": "FDH 12"}},
    )
    existing.ticket_type = "Cabinet Disconnection"
    db_session.flush()
    monkeypatch.setattr(
        infrastructure_ticket_selection,
        "allowed_kinds",
        lambda _db, _type: (infrastructure_ticket_selection.InfrastructureKind.manual,),
    )
    payload = web_service.build_ticket_create_payload(
        web_service.TicketFormValues(
            **_form_values(
                title="FDH 12 uplink down",
                description="Cabinet uplink has failed",
                ticket_type="Cabinet Disconnection",
            )
        )
    )
    selection = web_service.ManualInfrastructureSelection(
        name="FDH-12", request_key=uuid4()
    )
    actor_id = str(uuid4())

    def create(*, duplicate_override):
        return web_service.create_ticket_from_form(
            db_session,
            command=web_service.TicketFormCreateCommand(
                payload=payload,
                actor_id=actor_id,
                infrastructure=selection,
                infrastructure_permissions=frozenset(
                    {"support:ticket:create", "monitoring:write"}
                ),
                duplicate_override=duplicate_override,
            ),
            request=None,
            attachments=[],
        )

    with pytest.raises(web_service.DuplicateTicketWarningError) as warning:
        create(duplicate_override=False)
    assert [match.ticket_id for match in warning.value.result.matches] == [
        str(existing.id)
    ]
    created = create(duplicate_override=True)
    assert created.id != existing.id
    assert created.metadata_["duplicate_override"] is True
    assert created.metadata_["possible_duplicate_tickets"][0]["ticket_id"] == str(
        existing.id
    )
    assert create(duplicate_override=False).id == created.id


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
            command=web_service.TicketFormCreateCommand(
                payload=web_service.build_ticket_create_payload(
                    web_service.TicketFormValues(
                        **_form_values(
                            title="Router keeps rebooting",
                            subscriber_id=str(subscriber.id),
                            customer_account_id=str(subscriber.id),
                            ticket_type="Router Troubleshooting",
                        )
                    )
                ),
                actor_id=None,
            ),
            request=None,
            attachments=[],
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
        command=web_service.TicketFormCreateCommand(
            payload=web_service.build_ticket_create_payload(
                web_service.TicketFormValues(
                    **_form_values(
                        title="Router keeps rebooting",
                        subscriber_id=str(subscriber.id),
                        customer_account_id=str(subscriber.id),
                        ticket_type="Router Troubleshooting",
                    )
                )
            ),
            actor_id=None,
            duplicate_override=True,
        ),
        request=None,
        attachments=[],
    )

    assert ticket.id is not None
    metadata = ticket.metadata_ or {}
    assert metadata.get("duplicate_override") is True
    references = [
        item["ticket_id"] for item in metadata.get("possible_duplicate_tickets") or []
    ]
    assert str(existing.id) in references
