from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

from app.api import support as support_api
from app.models.support import Ticket, TicketChannel
from app.schemas.support import (
    TicketBulkUpdateItem,
    TicketBulkUpdateRequest,
    TicketCreate,
    TicketUpdate,
)
from app.services import support as support_service
from app.services import web_support_tickets
from scripts.seed import seed_rbac

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ticket(**overrides) -> Ticket:
    values = {
        "title": "Assignment update authority",
        "status": "open",
        "priority": "normal",
        "channel": TicketChannel.web,
        "is_active": True,
    }
    values.update(overrides)
    return Ticket(**values)


def _load_migration(filename: str):
    path = PROJECT_ROOT / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_permission_owner_call_accepts_assignment_fields(db_session):
    values = {
        "assigned_to_person_id": uuid4(),
        "technician_person_id": uuid4(),
        "ticket_manager_person_id": uuid4(),
        "site_coordinator_person_id": uuid4(),
        "service_team_id": uuid4(),
        "assignee_person_ids": [uuid4()],
    }
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    updated = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate.model_validate(values),
    )

    assert updated.assigned_to_person_id == values["assigned_to_person_id"]
    assert updated.technician_person_id == values["technician_person_id"]
    assert updated.ticket_manager_person_id == values["ticket_manager_person_id"]
    assert updated.site_coordinator_person_id == values["site_coordinator_person_id"]
    assert updated.service_team_id == values["service_team_id"]
    assert {row.person_id for row in updated.assignees} == set(
        values["assignee_person_ids"]
    )


def test_ticket_creation_accepts_explicit_assignment(db_session, subscriber):
    technician_id = uuid4()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Explicit assignment",
            subscriber_id=subscriber.id,
            technician_person_id=technician_id,
        ),
    )

    assert ticket.technician_person_id == technician_id


def test_manual_auto_assignment_is_an_update_action(db_session, monkeypatch):
    policy_assignee = uuid4()
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    def apply_policy(ticket, _db):
        ticket.assigned_to_person_id = policy_assignee
        return {"matched": True, "changes": {"assigned_to_person_id": policy_assignee}}

    monkeypatch.setattr(
        support_service.Tickets,
        "_apply_auto_assignment",
        staticmethod(apply_policy),
    )

    result = support_service.tickets.manual_auto_assign(db_session, str(ticket.id))

    assert result["matched"] is True
    assert db_session.get(Ticket, ticket.id).assigned_to_person_id == policy_assignee


def test_bulk_assignment_is_an_update_action(db_session):
    assignee_id = uuid4()
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    updated = support_service.tickets.bulk_update(
        db_session,
        TicketBulkUpdateRequest(
            items=[
                TicketBulkUpdateItem(
                    ticket_id=ticket.id,
                    assigned_to_person_id=assignee_id,
                )
            ]
        ),
    )

    assert updated[0].assigned_to_person_id == assignee_id


def test_api_patch_accepts_assignment_under_update_permission(db_session):
    technician_id = uuid4()
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    updated = support_api.update_ticket(
        ticket.id,
        TicketUpdate(technician_person_id=technician_id),
        auth={"principal_id": str(uuid4()), "principal_type": "system_user"},
        db=db_session,
    )

    assert updated.technician_person_id == technician_id


def test_web_quick_assignment_uses_ticket_update_authority(db_session):
    technician_id = uuid4()
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    updated = web_support_tickets.quick_update_ticket(
        db_session,
        request=None,
        ticket_id=str(ticket.id),
        actor_id=str(uuid4()),
        fields={"technician_person_id": str(technician_id)},
    )

    assert updated.technician_person_id == technician_id


def test_current_rbac_contract_does_not_seed_dedicated_assignment_permission():
    cleanup = _load_migration("574_remove_ticket_assignment_permission.py")

    assert cleanup.down_revision == "573_ticket_assignment_role_grants"
    assert cleanup.ASSIGN_PERMISSION_KEY == "support:ticket:assign"
    assert all(
        key != "support:ticket:assign" for key, _label in seed_rbac.DEFAULT_PERMISSIONS
    )
    assert all(
        "support:ticket:assign" not in permissions
        for permissions in seed_rbac.ROLE_PERMISSIONS.values()
    )
