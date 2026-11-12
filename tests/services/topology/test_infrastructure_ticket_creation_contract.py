"""The ticket owner admits only typed infrastructure creation paths."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.service_team import ServiceTeam
from app.schemas.support import TicketCreate
from app.services import infrastructure_ticket_selection, support, ticket_validation
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.topology import outage_work_order_handoff as handoff


@pytest.fixture(autouse=True)
def configured_infrastructure_type(monkeypatch):
    monkeypatch.setattr(
        infrastructure_ticket_selection,
        "allowed_kinds",
        lambda _db, _ticket_type: (
            infrastructure_ticket_selection.InfrastructureKind.cabinet,
            infrastructure_ticket_selection.InfrastructureKind.manual,
        ),
    )


def _payload(**overrides) -> TicketCreate:
    fields = {
        "title": "Cabinet link down",
        "description": "Investigate cabinet connectivity",
        "ticket_type": "Cabinet Disconnection",
        **overrides,
    }
    return TicketCreate.model_validate(fields)


def _command(
    payload: TicketCreate, *, name: str = "FDH 12"
) -> support.ManualInfrastructureTicketCreateCommand:
    actor_id = uuid4()
    return support.ManualInfrastructureTicketCreateCommand(
        payload=payload,
        name=name,
        actor_id=actor_id,
        permissions=frozenset({"support:ticket:create", "monitoring:write"}),
        context=CommandContext.system(
            actor=str(actor_id),
            scope="support.ticket:infrastructure_manual",
            reason="Operator selected manual infrastructure",
            idempotency_key=str(uuid4()),
        ),
    )


def test_generic_create_rejects_legacy_text_only_infrastructure(monkeypatch):
    monkeypatch.setattr(
        support,
        "current_command_definition",
        lambda _db: SimpleNamespace(owner="support.ticket_lifecycle", name="create"),
    )
    with pytest.raises(DomainError) as error:
        support.Tickets._stage_create(
            object(),
            _payload(metadata={"base_station_details": "FDH 12"}),
        )
    assert error.value.code == "infrastructure_selection_required"


def test_generic_customer_ticket_can_keep_a_configured_infrastructure_category(
    monkeypatch,
):
    monkeypatch.setattr(
        support,
        "current_command_definition",
        lambda _db: SimpleNamespace(owner="support.ticket_lifecycle", name="create"),
    )

    class ReachedValidation(Exception):
        pass

    def validate(_db, payload):
        assert payload.subscriber_id is not None
        raise ReachedValidation

    monkeypatch.setattr(ticket_validation, "validate_ticket_creation", validate)
    with pytest.raises(ReachedValidation):
        support.Tickets._stage_create(object(), _payload(subscriber_id=uuid4()))


def test_asset_ticket_retains_operator_selected_active_team(monkeypatch):
    team_id = uuid4()
    incident = SimpleNamespace(id=uuid4(), status="open")
    team = SimpleNamespace(id=team_id, is_active=True)
    captured = []

    class Query:
        def filter(self, *_args):
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            return incident

    db = SimpleNamespace(
        query=lambda _model: Query(),
        get=lambda model, _id: team if model is ServiceTeam else None,
    )
    monkeypatch.setattr(handoff, "infrastructure_link_for", lambda *_args: None)
    monkeypatch.setattr(handoff, "_incident_target_label", lambda *_args: "FDH 12")
    monkeypatch.setattr(
        handoff.service_team_composition,
        "resolve_routing_team",
        lambda *_args, **_kwargs: pytest.fail("selected team was replaced"),
    )
    monkeypatch.setattr(
        support.Tickets,
        "create",
        lambda _db, payload, **_kwargs: (
            captured.append(payload) or SimpleNamespace(id=uuid4(), number="INF-1")
        ),
    )
    monkeypatch.setattr(
        handoff,
        "link_infrastructure_ticket",
        lambda *_args, **_kwargs: SimpleNamespace(id=uuid4()),
    )
    monkeypatch.setattr(handoff, "stage_audit_event", lambda *_args, **_kwargs: None)
    actor_id = uuid4()
    result = handoff._create_infrastructure_ticket(
        db,
        handoff.OutageInfrastructureTicketIssueCommand(
            incident_id=incident.id,
            actor_id=actor_id,
            permissions=handoff.TICKET_CREATE_PERMISSIONS,
            context=CommandContext.system(
                actor=str(actor_id),
                scope=handoff.TICKET_CREATE_SCOPE,
                reason="Cabinet field repair",
                idempotency_key=str(uuid4()),
            ),
            ticket_payload=_payload(service_team_id=team_id),
        ),
    )
    assert not result.replayed
    assert captured[0].service_team_id == team_id
    team.is_active = False
    with pytest.raises(handoff.OutageWorkOrderHandoffError) as inactive:
        handoff._create_infrastructure_ticket(
            db,
            handoff.OutageInfrastructureTicketIssueCommand(
                incident_id=incident.id,
                actor_id=actor_id,
                permissions=handoff.TICKET_CREATE_PERMISSIONS,
                context=CommandContext.system(
                    actor=str(actor_id),
                    scope=handoff.TICKET_CREATE_SCOPE,
                    reason="Cabinet field repair",
                    idempotency_key=str(uuid4()),
                ),
                ticket_payload=_payload(service_team_id=team_id),
            ),
        )
    assert inactive.value.code == "selected_team_inactive"


def test_existing_outage_ticket_reports_conflicting_selected_team(monkeypatch):
    incident = SimpleNamespace(id=uuid4(), status="open")
    existing_link = SimpleNamespace(ticket_id=uuid4())
    existing_ticket = SimpleNamespace(
        ticket_type="Cabinet Disconnection", service_team_id=uuid4()
    )

    class Query:
        def filter(self, *_args):
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            return incident

    db = SimpleNamespace(
        query=lambda _model: Query(), get=lambda _model, _id: existing_ticket
    )
    monkeypatch.setattr(
        handoff, "infrastructure_link_for", lambda *_args: existing_link
    )
    actor_id = uuid4()
    with pytest.raises(handoff.OutageWorkOrderHandoffError) as conflict:
        handoff._create_infrastructure_ticket(
            db,
            handoff.OutageInfrastructureTicketIssueCommand(
                incident_id=incident.id,
                actor_id=actor_id,
                permissions=handoff.TICKET_CREATE_PERMISSIONS,
                context=CommandContext.system(
                    actor=str(actor_id),
                    scope=handoff.TICKET_CREATE_SCOPE,
                    reason="Cabinet field repair",
                    idempotency_key=str(uuid4()),
                ),
                ticket_payload=_payload(service_team_id=uuid4()),
            ),
        )
    assert conflict.value.code == "infrastructure_ticket_team_conflict"


def test_manual_command_records_name_without_reconciliation(monkeypatch):
    captured = []
    monkeypatch.setattr(
        support.Tickets,
        "_stage_create",
        lambda _db, payload, **_kwargs: (
            captured.append(payload) or SimpleNamespace(id=uuid4())
        ),
    )
    command = _command(_payload())
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
        scalar=lambda _statement: None,
    )
    result = support.Tickets.create_infrastructure_manual.__wrapped__(
        db, command=command
    )
    assert result.replayed is False
    assert captured[0].metadata_["infrastructure_selection"] == {
        "mode": "manual",
        "name": "FDH 12",
        "ticket_type": "Cabinet Disconnection",
    }
    assert "missing_inventory_reason" not in captured[0].metadata_
    assert "reconciliation" not in captured[0].metadata_


def test_manual_command_respects_configured_target_kinds(monkeypatch):
    monkeypatch.setattr(
        infrastructure_ticket_selection,
        "allowed_kinds",
        lambda _db, _ticket_type: (
            infrastructure_ticket_selection.InfrastructureKind.cabinet,
        ),
    )
    with pytest.raises(DomainError) as error:
        support.Tickets.create_infrastructure_manual.__wrapped__(
            object(), command=_command(_payload())
        )
    assert error.value.code == "infrastructure_selection_required"


def test_manual_request_key_replays_exact_ticket(monkeypatch):
    staged = []

    def stage(_db, payload, **_kwargs):
        record = SimpleNamespace(id=uuid4(), metadata_=payload.metadata_)
        staged.append(record)
        return record

    monkeypatch.setattr(support.Tickets, "_stage_create", stage)
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
        scalar=lambda _statement: staged[0] if staged else None,
    )
    command = _command(_payload())
    first = support.Tickets.create_infrastructure_manual.__wrapped__(
        db, command=command
    )
    second = support.Tickets.create_infrastructure_manual.__wrapped__(
        db, command=command
    )
    assert first.ticket is second.ticket
    assert second.replayed is True
    assert len(staged) == 1
    with pytest.raises(DomainError) as error:
        support.Tickets.create_infrastructure_manual.__wrapped__(
            db, command=replace(command, name="FDH 13")
        )
    assert error.value.code == "infrastructure_idempotency_conflict"


@pytest.mark.parametrize(
    ("payload", "name"),
    [
        (_payload(), ""),
        (_payload(description=""), "FDH 12"),
        (_payload(subscriber_id=uuid4()), "FDH 12"),
    ],
)
def test_manual_command_rejects_missing_name_description_or_customer(payload, name):
    with pytest.raises(DomainError):
        support.Tickets.create_infrastructure_manual.__wrapped__(
            object(), command=_command(payload, name=name)
        )
