from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

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


def _authorization(*, can_assign: bool):
    return support_service.TicketAssignmentAuthorization.human(can_assign=can_assign)


def _ticket(**overrides) -> Ticket:
    values = {
        "title": "Assignment authorization",
        "status": "open",
        "priority": "normal",
        "channel": TicketChannel.web,
        "is_active": True,
    }
    values.update(overrides)
    return Ticket(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("assigned_to_person_id", uuid4()),
        ("technician_person_id", uuid4()),
        ("ticket_manager_person_id", uuid4()),
        ("site_coordinator_person_id", uuid4()),
        ("service_team_id", uuid4()),
        ("assignee_person_ids", [uuid4()]),
    ],
)
def test_update_only_owner_call_rejects_each_assignment_field(
    db_session,
    field,
    value,
):
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    with pytest.raises(support_service.SupportTicketError) as exc:
        support_service.tickets.update(
            db_session,
            str(ticket.id),
            TicketUpdate.model_validate({field: value}),
            assignment_authorization=_authorization(can_assign=False),
        )

    assert exc.value.code == "ticket_assignment_permission_required"
    assert exc.value.details["permission"] == "support:ticket:assign"
    assert field in exc.value.details["assignment_fields"]


def test_update_only_owner_call_keeps_ordinary_edits_and_unchanged_assignment(
    db_session,
):
    assignee_id = uuid4()
    ticket = _ticket(assigned_to_person_id=assignee_id)
    db_session.add(ticket)
    db_session.commit()

    updated = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(priority="high", assigned_to_person_id=assignee_id),
        assignment_authorization=_authorization(can_assign=False),
    )

    assert updated.priority == "high"
    assert updated.assigned_to_person_id == assignee_id


def test_assignment_permission_allows_owner_assignment(db_session):
    technician_id = uuid4()
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    updated = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(technician_person_id=technician_id),
        assignment_authorization=_authorization(can_assign=True),
    )

    assert updated.technician_person_id == technician_id


def test_ticket_creation_requires_assign_only_for_explicit_assignment(
    db_session,
    subscriber,
):
    unassigned = support_service.tickets.create(
        db_session,
        TicketCreate(title="Unassigned creation", subscriber_id=subscriber.id),
        assignment_authorization=_authorization(can_assign=False),
    )
    assert unassigned.assigned_to_person_id is None

    with pytest.raises(support_service.SupportTicketError) as exc:
        support_service.tickets.create(
            db_session,
            TicketCreate(
                title="Explicit assignment",
                subscriber_id=subscriber.id,
                technician_person_id=uuid4(),
            ),
            assignment_authorization=_authorization(can_assign=False),
        )
    assert exc.value.code == "ticket_assignment_permission_required"


def test_system_assignment_policy_remains_an_explicit_owner_consequence(
    db_session,
    subscriber,
    monkeypatch,
):
    policy_assignee = uuid4()
    monkeypatch.setattr(
        support_service.Tickets,
        "_auto_assignment_enabled",
        staticmethod(lambda _db: True),
    )

    def apply_policy(ticket, _db):
        ticket.assigned_to_person_id = policy_assignee
        return {"matched": True, "changes": {"assigned_to_person_id": policy_assignee}}

    monkeypatch.setattr(
        support_service.Tickets,
        "_apply_auto_assignment",
        staticmethod(apply_policy),
    )

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(title="Policy assigned", subscriber_id=subscriber.id),
        assignment_authorization=_authorization(can_assign=False),
    )

    assert ticket.assigned_to_person_id == policy_assignee


def test_manual_auto_assignment_requires_assignment_permission(db_session):
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    with pytest.raises(support_service.SupportTicketError) as exc:
        support_service.tickets.manual_auto_assign(
            db_session,
            str(ticket.id),
            assignment_authorization=_authorization(can_assign=False),
        )

    assert exc.value.code == "ticket_assignment_permission_required"


def test_bulk_status_update_succeeds_but_bulk_assignment_is_rejected(db_session):
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    updated = support_service.tickets.bulk_update(
        db_session,
        TicketBulkUpdateRequest(
            items=[TicketBulkUpdateItem(ticket_id=ticket.id, priority="high")]
        ),
        assignment_authorization=_authorization(can_assign=False),
    )
    assert updated[0].priority == "high"

    with pytest.raises(support_service.SupportTicketError) as exc:
        support_service.tickets.bulk_update(
            db_session,
            TicketBulkUpdateRequest(
                items=[
                    TicketBulkUpdateItem(
                        ticket_id=ticket.id,
                        assigned_to_person_id=uuid4(),
                    )
                ]
            ),
            assignment_authorization=_authorization(can_assign=False),
        )
    assert exc.value.code == "ticket_assignment_permission_required"


def test_api_patch_maps_assignment_denial_to_forbidden(
    db_session,
    monkeypatch,
):
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()
    monkeypatch.setattr(support_api, "has_permission", lambda *_args: False)

    with pytest.raises(HTTPException) as exc:
        support_api.update_ticket(
            ticket.id,
            TicketUpdate(technician_person_id=uuid4()),
            auth={"principal_id": str(uuid4()), "principal_type": "system_user"},
            db=db_session,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ticket_assignment_permission_required"


def test_api_patch_keeps_ordinary_update_available_without_assign(
    db_session,
    monkeypatch,
):
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()
    monkeypatch.setattr(support_api, "has_permission", lambda *_args: False)

    updated = support_api.update_ticket(
        ticket.id,
        TicketUpdate(priority="high"),
        auth={"principal_id": str(uuid4()), "principal_type": "system_user"},
        db=db_session,
    )

    assert updated.priority == "high"


def test_forged_web_assignment_submission_is_rejected(db_session):
    ticket = _ticket()
    db_session.add(ticket)
    db_session.commit()

    with pytest.raises(support_service.SupportTicketError) as exc:
        web_support_tickets.quick_update_ticket(
            db_session,
            request=None,
            ticket_id=str(ticket.id),
            actor_id=str(uuid4()),
            fields={"technician_person_id": str(uuid4())},
            assignment_authorization=_authorization(can_assign=False),
        )

    assert exc.value.code == "ticket_assignment_permission_required"


def _load_migration(filename: str):
    path = PROJECT_ROOT / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rbac_contract_keeps_project_roles_unassigned_and_technical_support_enabled():
    technical_support = _load_migration("186_seed_technical_support_role.py")
    project = _load_migration("193_role_scope_cleanup_project_role.py")
    support_assignment = _load_migration("572_ticket_assignment_role_grants.py")

    assert "support:ticket:assign" in technical_support.PERMISSION_KEYS
    assert "support:ticket:update" in technical_support.PERMISSION_KEYS
    assert "support:ticket:assign" not in project.PERMISSIONS
    assert "support:ticket:update" not in project.PERMISSIONS
    assert support_assignment.ROLE_NAMES == ("support", "Technical support")
    assert "support:ticket:assign" in seed_rbac.ROLE_PERMISSIONS["support"]
    assert (
        "support:ticket:assign"
        not in seed_rbac.ROLE_PERMISSIONS["customer_experience_manager"]
    )
    assert (
        "support:ticket:update"
        in seed_rbac.ROLE_PERMISSIONS["customer_experience_manager"]
    )
