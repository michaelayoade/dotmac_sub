"""Automation coordinator for vendor installation-project transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.vendor_routes import InstallationProject
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.vendor_project_lifecycle import (
    StageVendorProjectTransition,
    stage_project_transition,
)

OWNER = "operations.vendor_project_automation"
_TRANSITION = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation-driven vendor project transitions",
    name="transition_vendor_project_from_automation",
)


class VendorAutomationStatus(StrEnum):
    in_progress = "in_progress"
    completed = "completed"


@dataclass(frozen=True, slots=True)
class TransitionVendorProjectFromAutomationCommand:
    context: CommandContext
    project_id: UUID
    status: VendorAutomationStatus


def transition_vendor_project_from_automation(
    db: Session, command: TransitionVendorProjectFromAutomationCommand
) -> UUID:
    """Run a vendor transition as an owner command around the participant."""

    def operation() -> UUID:
        project = db.scalar(
            select(InstallationProject)
            .where(InstallationProject.id == command.project_id)
            .with_for_update()
        )
        if project is None or not project.is_active:
            raise DomainError(
                code=f"{OWNER}.not_found",
                message="Vendor installation project not found.",
                details={"project_id": str(command.project_id)},
            )
        if project.assigned_vendor_id is None:
            raise DomainError(
                code=f"{OWNER}.vendor_assignment_required",
                message="A vendor must be assigned before automation can transition the project.",
                details={"project_id": str(command.project_id)},
            )
        action = (
            "start"
            if command.status is VendorAutomationStatus.in_progress
            else "complete"
        )
        stage_project_transition(
            db,
            StageVendorProjectTransition(
                project_id=str(project.id),
                vendor_id=str(project.assigned_vendor_id),
                action=action,
                actor_id=command.context.actor,
                actor_type="automation",
            ),
        )
        return project.id

    return execute_owner_command(
        db,
        definition=_TRANSITION,
        context=command.context,
        operation=operation,
    )


__all__ = [
    "TransitionVendorProjectFromAutomationCommand",
    "VendorAutomationStatus",
    "transition_vendor_project_from_automation",
]
