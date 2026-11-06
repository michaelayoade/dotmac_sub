"""The ticket owner admits only typed infrastructure creation paths."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.support import TicketCreate
from app.services import support
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


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
