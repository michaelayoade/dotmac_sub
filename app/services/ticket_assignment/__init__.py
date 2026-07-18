"""Rule-based assignment services for native tickets and projects."""

from app.services.ticket_assignment.engine import (
    AssignmentResult,
    auto_assign_project,
    auto_assign_ticket,
    auto_assign_ticket_all,
    find_authoritative_project_creation_rule,
)

__all__ = [
    "AssignmentResult",
    "auto_assign_project",
    "auto_assign_ticket",
    "auto_assign_ticket_all",
    "find_authoritative_project_creation_rule",
]
