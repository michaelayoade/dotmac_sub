"""Typed Automation Center adapter for native work-order status changes."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas.dispatch import WorkOrderHeaderUpdate
from app.services.field.work_order_status import WorkOrderStatus
from app.services.owner_commands import CommandContext
from app.services.work_order_commands import WorkOrderCommands


@dataclass(frozen=True, slots=True)
class AutomationWorkOrderStatusCommand:
    context: CommandContext
    work_order_id: UUID
    status: WorkOrderStatus


def set_work_order_status_from_automation(
    db: Session, command: AutomationWorkOrderStatusCommand
) -> None:
    """Stage a native status change inside the Automation owner transaction."""

    WorkOrderCommands.update_header(
        db,
        str(command.work_order_id),
        WorkOrderHeaderUpdate(status=command.status.value),
        request_id=str(command.context.command_id),
        commit=False,
    )


__all__ = [
    "AutomationWorkOrderStatusCommand",
    "set_work_order_status_from_automation",
]
