"""Authorized bulk interaction projection for the admin support-ticket list."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.support import Ticket
from app.services import web_support_ticket_bulk
from app.services.auth_dependencies import has_permission
from app.services.bulk_actions import BulkActionDefinition, BulkResourceDefinition

SUPPORT_TICKET_BULK_ACTION_DEFINITION = BulkResourceDefinition(
    key="support_tickets",
    actions=(
        BulkActionDefinition(
            key="update",
            label="Update tickets",
            description=(
                "Change status, priority, or primary assignee for selected tickets."
            ),
            permission="support:ticket:update",
            tone="info",
        ),
    ),
)


def build_support_ticket_bulk_action_contract(
    db: Session,
    *,
    auth: dict,
    tickets: Sequence[Ticket],
) -> dict[str, object]:
    """Project authorization and page-row eligibility without copying policy."""

    authorized_permissions = {
        action.permission
        for action in SUPPORT_TICKET_BULK_ACTION_DEFINITION.actions
        if auth and has_permission(auth, db, action.permission)
    }
    contract = SUPPORT_TICKET_BULK_ACTION_DEFINITION.project(
        authorized_permissions=authorized_permissions
    ).as_dict()
    actions = contract["actions"]
    assert isinstance(actions, list)
    for action in actions:
        assert isinstance(action, dict)
        eligible_ids: list[str] = []
        ineligible_reasons: dict[str, str] = {}
        for ticket in tickets:
            ticket_id = str(ticket.id)
            reason = web_support_ticket_bulk.support_ticket_bulk_static_ineligibility(
                ticket
            )
            if reason:
                ineligible_reasons[ticket_id] = reason
            else:
                eligible_ids.append(ticket_id)
        action["eligible_ids"] = eligible_ids
        action["ineligible_reasons"] = ineligible_reasons
    return contract
