"""Static runtime adapters from automation actions to typed command owners."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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


def _suspend_support_ticket_service_for_sla_breach(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    """Execute an immutable legacy action key using the canonical Pause owner."""

    from app.models.subscription_pause import (
        SubscriptionPauseBillingPolicy,
        SubscriptionPauseResumePolicy,
    )
    from app.services.ticket_sla_service_automation import (
        PauseTicketServiceForSlaBreachCommand,
        TicketSlaServiceSelectionPolicy,
        pause_unique_active_service_for_ticket_sla_breach,
    )

    if command.target.entity_type != "support.ticket":
        raise AutomationActionExecutorError(
            "The retired ticket SLA action received the wrong target type."
        )
    if command.inputs:
        raise AutomationActionExecutorError(
            "The retired ticket SLA action does not accept inputs."
        )
    outcome = pause_unique_active_service_for_ticket_sla_breach(
        db,
        PauseTicketServiceForSlaBreachCommand(
            ticket_id=command.target.entity_id,
            event_id=command.event_id,
            rule_id=command.rule_id,
            rule_version_id=command.rule_version_id,
            step_index=command.step_index,
            selection_policy=(
                TicketSlaServiceSelectionPolicy.unique_active_subscription
            ),
            resume_policy=(
                SubscriptionPauseResumePolicy.manual_after_ticket_resolution
            ),
            billing_policy=(
                SubscriptionPauseBillingPolicy.extend_by_effective_pause_duration
            ),
            context=command.context,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code=(
            "support_ticket_service_pause_replayed"
            if outcome.replayed
            else "support_ticket_service_paused"
        ),
    )


def _pause_support_ticket_service_for_sla_breach(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.models.subscription_pause import (
        SubscriptionPauseBillingPolicy,
        SubscriptionPauseResumePolicy,
    )
    from app.services.ticket_sla_service_automation import (
        PauseTicketServiceForSlaBreachCommand,
        TicketSlaServiceSelectionPolicy,
        pause_unique_active_service_for_ticket_sla_breach,
    )

    if command.target.entity_type != "support.ticket":
        raise AutomationActionExecutorError(
            "The ticket SLA service-pause action received the wrong target type."
        )
    values = {item.key: item.value for item in command.inputs}
    try:
        selection_policy = TicketSlaServiceSelectionPolicy(
            str(values["service_selection_policy"])
        )
        resume_policy = SubscriptionPauseResumePolicy(str(values["resume_policy"]))
        billing_policy = SubscriptionPauseBillingPolicy(str(values["billing_policy"]))
    except (KeyError, ValueError) as exc:
        raise AutomationActionExecutorError(
            "The ticket SLA service-pause action configuration is invalid."
        ) from exc
    outcome = pause_unique_active_service_for_ticket_sla_breach(
        db,
        PauseTicketServiceForSlaBreachCommand(
            ticket_id=command.target.entity_id,
            event_id=command.event_id,
            rule_id=command.rule_id,
            rule_version_id=command.rule_version_id,
            step_index=command.step_index,
            selection_policy=selection_policy,
            resume_policy=resume_policy,
            billing_policy=billing_policy,
            context=command.context,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code=(
            "support_ticket_service_pause_replayed"
            if outcome.replayed
            else "support_ticket_service_paused"
        ),
    )


def _set_project_status(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.models.project import ProjectStatus
    from app.schemas.project import ProjectUpdate
    from app.services.projects import Projects

    if command.target.entity_type != "operations.project":
        raise AutomationActionExecutorError(
            "The project status action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "status"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'status' input."
        )
    try:
        status = ProjectStatus(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action project status is no longer supported."
        ) from exc
    Projects.update(
        db,
        str(command.target.entity_id),
        ProjectUpdate(status=status),
        context=command.context,
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="project_status_set",
    )


def _enqueue_material_request_cancellation(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.services.field.material_requests import (
        consume_material_request_cancellation_requested,
    )

    if command.target.entity_type != "operations.material_request":
        raise AutomationActionExecutorError(
            "The material-request cancellation action received the wrong target type."
        )
    consume_material_request_cancellation_requested(
        db,
        material_request_id=str(command.target.entity_id),
        event_id=command.event_id,
        context=command.context,
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="material_request_cancellation_enqueued",
    )


def _set_sales_lead_status(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.models.sales import LeadStatus
    from app.services.sales.lead_authoring import (
        AutomationLeadStatusCommand,
        set_lead_status_from_automation,
    )

    if command.target.entity_type != "sales.lead":
        raise AutomationActionExecutorError(
            "The lead status action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "status"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'status' input."
        )
    try:
        status = LeadStatus(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action lead status is no longer supported."
        ) from exc
    set_lead_status_from_automation(
        db,
        AutomationLeadStatusCommand(
            context=command.context,
            lead_id=command.target.entity_id,
            status=status,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="sales_lead_status_set",
    )


def _apply_customer_status_action(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.services import account_status_commands
    from app.services.db_session_adapter import db_session_adapter

    if command.target.entity_type != "customer.account":
        raise AutomationActionExecutorError(
            "The customer status action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "action"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'action' input."
        )
    try:
        action = account_status_commands.AccountStatusAction(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action customer status is no longer supported."
        ) from exc
    preview = account_status_commands.preview_account_status_change(
        db,
        account_status_commands.PreviewAccountStatusRequest(
            account_id=command.target.entity_id,
            action=action,
        ),
    )
    db_session_adapter.release_read_transaction(db)
    account_status_commands.confirm_account_status_change(
        db,
        account_status_commands.ConfirmAccountStatusCommand(
            context=replace(
                command.context,
                scope=account_status_commands.ACCOUNT_STATUS_WRITE_SCOPE,
            ),
            account_id=command.target.entity_id,
            action=action,
            expected_preview_fingerprint=preview.fingerprint,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="customer_account_status_action_applied",
    )


def _set_sales_quote_status(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.models.sales import QuoteStatus
    from app.services.sales.quote_authoring import (
        AutomationQuoteStatusCommand,
        set_quote_status_from_automation,
    )

    if command.target.entity_type != "sales.quote":
        raise AutomationActionExecutorError(
            "The quote status action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "status"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'status' input."
        )
    try:
        status = QuoteStatus(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action quote status is no longer supported."
        ) from exc
    set_quote_status_from_automation(
        db,
        AutomationQuoteStatusCommand(
            context=command.context,
            quote_id=command.target.entity_id,
            status=status,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="sales_quote_status_set",
    )


def _set_sales_order_status(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.models.sales import SalesOrderStatus
    from app.services.sales_orders import (
        AutomationSalesOrderStatusCommand,
        set_sales_order_status_from_automation,
    )

    if command.target.entity_type != "sales.sales_order":
        raise AutomationActionExecutorError(
            "The sales-order status action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "status"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'status' input."
        )
    try:
        status = SalesOrderStatus(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action sales-order status is no longer supported."
        ) from exc
    set_sales_order_status_from_automation(
        db,
        AutomationSalesOrderStatusCommand(
            context=command.context,
            sales_order_id=command.target.entity_id,
            status=status,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="sales_order_status_set",
    )


def _set_vendor_project_status(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.services.vendor_project_automation import (
        TransitionVendorProjectFromAutomationCommand,
        VendorAutomationStatus,
        transition_vendor_project_from_automation,
    )

    if command.target.entity_type != "operations.vendor":
        raise AutomationActionExecutorError(
            "The vendor-project status action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "status"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'status' input."
        )
    try:
        status = VendorAutomationStatus(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action vendor-project status is no longer supported."
        ) from exc
    transition_vendor_project_from_automation(
        db,
        TransitionVendorProjectFromAutomationCommand(
            context=command.context,
            project_id=command.target.entity_id,
            status=status,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="vendor_project_status_set",
    )


def _set_work_order_status(
    db: Session, command: ExecuteAutomationActionCommand
) -> AutomationActionOutcome:
    from app.services.field.work_order_status import WorkOrderStatus
    from app.services.work_order_automation import (
        AutomationWorkOrderStatusCommand,
        set_work_order_status_from_automation,
    )

    if command.target.entity_type != "operations.work_order":
        raise AutomationActionExecutorError(
            "The work-order status action received the wrong target type."
        )
    values = [item.value for item in command.inputs if item.key == "status"]
    if len(values) != 1:
        raise AutomationActionExecutorError(
            "Automation action requires exactly one 'status' input."
        )
    try:
        status = WorkOrderStatus(str(values[0]))
    except ValueError as exc:
        raise AutomationActionExecutorError(
            "Automation action work-order status is no longer supported."
        ) from exc
    set_work_order_status_from_automation(
        db,
        AutomationWorkOrderStatusCommand(
            context=command.context,
            work_order_id=command.target.entity_id,
            status=status,
        ),
    )
    return AutomationActionOutcome(
        disposition=AutomationActionDisposition.succeeded,
        outcome_code="work_order_status_set",
    )


# Module-adapter PRs add exact key -> typed adapter entries here. The immutable
# mapping prevents runtime registration from turning a configuration change
# into executable code admission.
_ACTION_EXECUTORS: Mapping[str, AutomationActionExecutor] = MappingProxyType(
    {
        "support.ticket.assign_service_team": _assign_support_ticket_service_team,
        "support.ticket.set_priority": _set_support_ticket_priority,
        "support.ticket.suspend_unique_active_service": (
            _suspend_support_ticket_service_for_sla_breach
        ),
        "support.ticket.pause_unique_active_service": (
            _pause_support_ticket_service_for_sla_breach
        ),
        "operations.project.set_status": _set_project_status,
        "operations.material_request.enqueue_cancellation": _enqueue_material_request_cancellation,
        "sales.lead.set_status": _set_sales_lead_status,
        "sales.quote.set_status": _set_sales_quote_status,
        "sales.sales_order.set_status": _set_sales_order_status,
        "customer.account.set_status": _apply_customer_status_action,
        "operations.work_order.set_status": _set_work_order_status,
        "operations.vendor.set_status": _set_vendor_project_status,
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
