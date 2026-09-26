"""Static runtime adapters from automation actions to typed command owners."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import automation_capabilities
from app.services.owner_commands import CommandContext


class AutomationActionDisposition(StrEnum):
    succeeded = "succeeded"
    skipped = "skipped"


@dataclass(frozen=True, slots=True)
class AutomationTargetReference:
    entity_type: str
    entity_id: UUID


@dataclass(frozen=True, slots=True)
class AutomationActionInputValue:
    key: str
    value: object


@dataclass(frozen=True, slots=True)
class ExecuteAutomationActionCommand:
    tenant_id: UUID
    event_id: UUID
    rule_id: UUID
    rule_version_id: UUID
    step_index: int
    target: AutomationTargetReference
    inputs: tuple[AutomationActionInputValue, ...]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationActionOutcome:
    disposition: AutomationActionDisposition
    outcome_code: str


AutomationActionExecutor = Callable[
    [Session, ExecuteAutomationActionCommand], AutomationActionOutcome
]


class AutomationActionExecutorError(ValueError):
    pass


def _uuid_input(inputs: tuple[AutomationActionInputValue, ...], *, key: str) -> UUID:
    values = [item.value for item in inputs if item.key == key]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            f"Automation action requires exactly one {key!r} input."
        )
    try:
        return UUID(str(values[0]))
    except (TypeError, ValueError) as exc:
        raise AutomationActionExecutorError(
            f"Automation action input {key!r} must be a UUID."
        ) from exc


def _assign_support_ticket_service_team(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.services.support import Tickets
    from app.services.support_ticket_contracts import (
        AssignTicketServiceTeamFromAutomationCommand,
    )

    if command.target.entity_type != "support.ticket":
        raise AutomationActionExecutorError(
            "The support ticket assignment action received the wrong target type."
        )
    Tickets.assign_ticket_service_team_from_automation(
        db,
        command=AssignTicketServiceTeamFromAutomationCommand(
            ticket_id=command.target.entity_id,
            service_team_id=_uuid_input(command.inputs, key="service_team_id"),
            event_id=command.event_id,
            rule_id=command.rule_id,
            rule_version_id=command.rule_version_id,
            step_index=command.step_index,
            context=command.context,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="support_ticket_service_team_assigned",
    )


def _set_support_ticket_priority(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.models.support import TicketPriority
    from app.services.support import Tickets
    from app.services.support_ticket_contracts import (
        SetTicketPriorityFromAutomationCommand,
    )

    if command.target.entity_type != "support.ticket":
        raise AutomationActionExecutorError(
            "The support ticket priority action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "priority"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'priority' input."
        )
    try:
        priority = TicketPriority(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action priority is no longer supported."
        ) from exc
    Tickets.set_ticket_priority_from_automation(
        db,
        command=SetTicketPriorityFromAutomationCommand(
            ticket_id=command.target.entity_id,
            priority=priority,
            event_id=command.event_id,
            rule_id=command.rule_id,
            rule_version_id=command.rule_version_id,
            step_index=command.step_index,
            context=command.context,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="support_ticket_priority_set",
    )


# Module-adapter PRs add exact key -> typed adapter entries here. The immutable
# mapping prevents runtime registration from turning a configuration change
# into executable code admission.
_ACTION_EXECUTORS: Mapping[str, AutomationActionExecutor] = MappingProxyType(
    {
        "support.ticket.assign_service_team": _assign_support_ticket_service_team,
        "support.ticket.set_priority": _set_support_ticket_priority,
    }
)


def action_executor(action_key: str) -> AutomationActionExecutor:
    executor = _ACTION_EXECUTORS.get(action_key)
    if executor is None:
        raise AutomationActionExecutorError(
            f"Automation action {action_key!r} has no runtime executor."
        )
    return executor


def runtime_registry_errors() -> tuple[str, ...]:
    declared = {
        action.key
        for module in automation_capabilities.registered_module_manifests()
        for action in module.actions
    }
    runtime_declared = {
        action.key
        for module in automation_capabilities.registered_module_manifests()
        for action in module.actions
        if action.runtime_enabled
    }
    executable = set(_ACTION_EXECUTORS)
    errors = [
        *(
            f"declared action {key!r} has no executor"
            for key in sorted(runtime_declared - executable)
        ),
        *(
            f"executor {key!r} has no capability declaration"
            for key in sorted(executable - declared)
        ),
    ]
    return tuple(errors)


def require_valid_runtime_registry() -> None:
    errors = runtime_registry_errors()
    if errors:
        raise AutomationActionExecutorError("; ".join(errors))


__all__ = [
    "AutomationActionDisposition",
    "AutomationActionExecutorError",
    "AutomationActionInputValue",
    "AutomationActionOutcome",
    "AutomationTargetReference",
    "ExecuteAutomationActionCommand",
    "action_executor",
    "require_valid_runtime_registry",
    "runtime_registry_errors",
]
