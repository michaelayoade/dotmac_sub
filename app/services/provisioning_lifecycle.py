"""Authoritative provisioning-readiness and service-order activation owner.

Provisioning workflow execution records technical observations.  This owner
decides whether those observations, the native project/task graph, field-work
evidence, and the established IP-assignment fact permit activation.  Network
systems remain projection transports; they confirm the exact service order
only after their existing activation work succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment
from app.models.project import Project, ProjectTask, ProjectTaskStatus
from app.models.provisioning import (
    ProvisioningReadinessCheck,
    ProvisioningReadinessCheckKind,
    ProvisioningReadinessCheckResult,
    ProvisioningReadinessDecision,
    ProvisioningReadinessDecisionStatus,
    ProvisioningRun,
    ProvisioningRunStatus,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
)
from app.models.subscription_change import SubscriptionChangeRequest
from app.models.work_order import WorkOrder
from app.services import service_order_lifecycle
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.field.work_order_status import WORK_ORDER_TERMINAL_VALUES
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "operations.provisioning_lifecycle"

_EVALUATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="provisioning readiness and activation request decisions",
    name="evaluate_readiness",
)
_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="service-order activation confirmation",
    name="confirm_activation",
)


class ProvisioningLifecycleError(DomainError):
    """Stable transport-neutral provisioning lifecycle failure."""


def _error(code: str, message: str, **details: object) -> ProvisioningLifecycleError:
    return ProvisioningLifecycleError(
        code=f"{OWNER}.{code}", message=message, details=details
    )


@dataclass(frozen=True)
class EvaluateReadinessCommand:
    context: CommandContext
    service_order_id: UUID
    provisioning_run_id: UUID


@dataclass(frozen=True)
class ConfirmActivationCommand:
    context: CommandContext
    service_order_id: UUID
    subscription_id: UUID


@dataclass(frozen=True)
class ReadinessCheck:
    kind: ProvisioningReadinessCheckKind
    result: ProvisioningReadinessCheckResult
    reason_code: str
    source_type: str
    source_id: UUID | None = None


@dataclass(frozen=True)
class ReadinessOutcome:
    decision_id: UUID
    service_order_id: UUID
    status: ProvisioningReadinessDecisionStatus
    reason_code: str
    checks: tuple[ReadinessCheck, ...]
    command_id: UUID
    correlation_id: UUID
    decided_at: datetime


def _to_outcome(decision: ProvisioningReadinessDecision) -> ReadinessOutcome:
    return ReadinessOutcome(
        decision_id=decision.id,
        service_order_id=decision.service_order_id,
        status=decision.status,
        reason_code=decision.reason_code,
        checks=tuple(
            ReadinessCheck(
                kind=check.kind,
                result=check.result,
                reason_code=check.reason_code,
                source_type=check.source_type,
                source_id=check.source_id,
            )
            for check in decision.checks
        ),
        command_id=decision.command_id,
        correlation_id=decision.correlation_id,
        decided_at=decision.decided_at,
    )


def _decision_for_command(
    db: Session, command_id: UUID
) -> ProvisioningReadinessDecision | None:
    return db.scalar(
        select(ProvisioningReadinessDecision)
        .options(selectinload(ProvisioningReadinessDecision.checks))
        .where(ProvisioningReadinessDecision.command_id == command_id)
    )


def _validate_replay_scope(
    decision: ProvisioningReadinessDecision,
    *,
    service_order_id: UUID,
    provisioning_run_id: UUID | None = None,
) -> None:
    """Fail closed when one idempotency key is reused for another command."""

    if decision.service_order_id != service_order_id or (
        provisioning_run_id is not None
        and decision.provisioning_run_id != provisioning_run_id
    ):
        raise _error(
            "command_replay_conflict",
            "Command id was already used for a different provisioning scope.",
            command_id=str(decision.command_id),
        )


def latest_readiness(db: Session, service_order_id: UUID) -> ReadinessOutcome | None:
    """Return the latest persisted decision; never derive a second policy path."""

    decision = db.scalar(
        select(ProvisioningReadinessDecision)
        .options(selectinload(ProvisioningReadinessDecision.checks))
        .where(ProvisioningReadinessDecision.service_order_id == service_order_id)
        .order_by(
            ProvisioningReadinessDecision.decided_at.desc(),
            ProvisioningReadinessDecision.created_at.desc(),
        )
        .limit(1)
    )
    return _to_outcome(decision) if decision is not None else None


def _check(
    kind: ProvisioningReadinessCheckKind,
    result: ProvisioningReadinessCheckResult,
    reason_code: str,
    source_type: str,
    source_id: UUID | None = None,
) -> ReadinessCheck:
    return ReadinessCheck(kind, result, reason_code, source_type, source_id)


def _evaluate_facts(
    db: Session, order: ServiceOrder, run: ProvisioningRun
) -> tuple[ReadinessCheck, ...]:
    checks: list[ReadinessCheck] = []
    run_passed = run.status == ProvisioningRunStatus.success
    checks.append(
        _check(
            ProvisioningReadinessCheckKind.provisioning_run,
            (
                ProvisioningReadinessCheckResult.passed
                if run_passed
                else ProvisioningReadinessCheckResult.failed
            ),
            "provisioning_run_succeeded" if run_passed else "provisioning_run_failed",
            "provisioning_run",
            run.id,
        )
    )

    requires_project = order.order_type == ServiceOrderType.new_install
    project = db.get(Project, order.project_id) if order.project_id else None
    project_valid = bool(
        project and project.is_active and project.subscriber_id == order.subscriber_id
    )
    checks.append(
        _check(
            ProvisioningReadinessCheckKind.project_binding,
            (
                ProvisioningReadinessCheckResult.passed
                if project_valid
                else (
                    ProvisioningReadinessCheckResult.failed
                    if requires_project
                    else ProvisioningReadinessCheckResult.not_applicable
                )
            ),
            (
                "project_binding_valid"
                if project_valid
                else "project_binding_required"
                if requires_project
                else "project_not_required"
            ),
            "project",
            project.id if project else None,
        )
    )

    task = (
        db.get(ProjectTask, order.activation_project_task_id)
        if order.activation_project_task_id
        else None
    )
    task_valid = bool(
        task
        and project is not None
        and task.is_active
        and project_valid
        and task.project_id == project.id
        and task.status == ProjectTaskStatus.done.value
    )
    checks.append(
        _check(
            ProvisioningReadinessCheckKind.activation_task,
            (
                ProvisioningReadinessCheckResult.passed
                if task_valid
                else (
                    ProvisioningReadinessCheckResult.failed
                    if requires_project
                    else ProvisioningReadinessCheckResult.not_applicable
                )
            ),
            (
                "activation_task_completed"
                if task_valid
                else "activation_task_incomplete"
                if requires_project
                else "activation_task_not_required"
            ),
            "project_task",
            task.id if task else None,
        )
    )

    relocation_request = db.scalar(
        select(SubscriptionChangeRequest).where(
            SubscriptionChangeRequest.service_order_id == order.id
        )
    )
    requires_field_work = requires_project or relocation_request is not None
    work_orders: list[WorkOrder]
    if relocation_request is not None and relocation_request.work_order_id is not None:
        relocation_work_order = db.get(WorkOrder, relocation_request.work_order_id)
        work_orders = (
            [relocation_work_order]
            if relocation_work_order is not None and relocation_work_order.is_active
            else []
        )
    else:
        work_orders = (
            list(
                db.scalars(
                    select(WorkOrder).where(
                        WorkOrder.project_task_id == task.id,
                        WorkOrder.is_active.is_(True),
                    )
                )
            )
            if task is not None
            else []
        )
    field_passed = not work_orders or (
        all(item.status in WORK_ORDER_TERMINAL_VALUES for item in work_orders)
        and any(item.status == "completed" for item in work_orders)
    )
    checks.append(
        _check(
            ProvisioningReadinessCheckKind.field_work,
            (
                ProvisioningReadinessCheckResult.passed
                if ((task_valid or relocation_request is not None) and field_passed)
                else (
                    ProvisioningReadinessCheckResult.failed
                    if requires_field_work
                    else ProvisioningReadinessCheckResult.not_applicable
                )
            ),
            (
                "field_work_completed"
                if work_orders and field_passed
                else "field_work_not_required"
                if (task_valid or relocation_request is not None) and not work_orders
                else "field_work_incomplete"
                if requires_field_work
                else "field_work_not_required"
            ),
            (
                "subscription_change_work_order"
                if relocation_request is not None
                else "project_task_work_orders"
            ),
            (
                relocation_request.work_order_id
                if relocation_request is not None
                else task.id
                if task
                else None
            ),
        )
    )

    ip_assignment = None
    if order.subscription_id is not None:
        ip_assignment = db.scalar(
            select(IPAssignment)
            .where(
                IPAssignment.subscription_id == order.subscription_id,
                IPAssignment.is_active.is_(True),
            )
            .order_by(IPAssignment.created_at.asc())
            .limit(1)
        )
    checks.append(
        _check(
            ProvisioningReadinessCheckKind.ip_assignment,
            (
                ProvisioningReadinessCheckResult.passed
                if ip_assignment is not None
                else ProvisioningReadinessCheckResult.failed
            ),
            (
                "active_ip_assignment_present"
                if ip_assignment is not None
                else "active_ip_assignment_missing"
            ),
            "ip_assignment",
            ip_assignment.id if ip_assignment else None,
        )
    )
    return tuple(checks)


def _append_decision(
    db: Session,
    *,
    order: ServiceOrder,
    run_id: UUID | None,
    context: CommandContext,
    status: ProvisioningReadinessDecisionStatus,
    reason_code: str,
    checks: tuple[ReadinessCheck, ...],
) -> ProvisioningReadinessDecision:
    decision = ProvisioningReadinessDecision(
        service_order_id=order.id,
        provisioning_run_id=run_id,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        status=status,
        reason_code=reason_code,
        actor=context.actor,
        decided_at=datetime.now(UTC),
    )
    decision.checks = [
        ProvisioningReadinessCheck(
            kind=item.kind,
            result=item.result,
            reason_code=item.reason_code,
            source_type=item.source_type,
            source_id=item.source_id,
            observed_at=datetime.now(UTC),
        )
        for item in checks
    ]
    db.add(decision)
    db.flush()
    return decision


def evaluate_readiness(
    db: Session, command: EvaluateReadinessCommand
) -> ReadinessOutcome:
    """Record technical outcome, decide readiness, and request activation once."""

    def operation() -> ReadinessOutcome:
        replay = _decision_for_command(db, command.context.command_id)
        if replay is not None:
            _validate_replay_scope(
                replay,
                service_order_id=command.service_order_id,
                provisioning_run_id=command.provisioning_run_id,
            )
            return _to_outcome(replay)
        order = db.scalar(
            select(ServiceOrder)
            .where(ServiceOrder.id == command.service_order_id)
            .with_for_update()
        )
        if order is None:
            raise _error("service_order_not_found", "Service order was not found.")
        run = db.scalar(
            select(ProvisioningRun)
            .where(ProvisioningRun.id == command.provisioning_run_id)
            .with_for_update()
        )
        if run is None or run.service_order_id != order.id:
            raise _error(
                "run_scope_mismatch",
                "Provisioning run does not belong to the service order.",
            )
        if order.status in {ServiceOrderStatus.canceled, ServiceOrderStatus.active}:
            raise _error(
                "invalid_order_state",
                "Service order cannot be evaluated in its current state.",
                status=order.status.value,
            )

        checks = _evaluate_facts(db, order, run)
        if run.status == ProvisioningRunStatus.failed:
            decision = _append_decision(
                db,
                order=order,
                run_id=run.id,
                context=command.context,
                status=ProvisioningReadinessDecisionStatus.failed,
                reason_code="provisioning_run_failed",
                checks=checks,
            )
            service_order_lifecycle.record_provisioning_result(
                db,
                service_order_id=order.id,
                succeeded=False,
                readiness_decision_id=decision.id,
                actor_id=command.context.actor,
                reason="provisioning_run_failed",
            )
            return _to_outcome(decision)
        if run.status != ProvisioningRunStatus.success:
            raise _error(
                "run_not_terminal",
                "Provisioning run has not reached a terminal state.",
                status=run.status.value,
            )

        failed_checks = tuple(
            item
            for item in checks
            if item.result == ProvisioningReadinessCheckResult.failed
        )
        if failed_checks:
            if order.status != ServiceOrderStatus.provisioning:
                raise _error(
                    "invalid_order_state",
                    "A blocked readiness decision requires an in-flight service order.",
                    status=order.status.value,
                )
            decision = _append_decision(
                db,
                order=order,
                run_id=run.id,
                context=command.context,
                status=ProvisioningReadinessDecisionStatus.blocked,
                reason_code=failed_checks[0].reason_code,
                checks=checks,
            )
            return _to_outcome(decision)

        if order.subscription_id is None:
            raise _error(
                "subscription_required",
                "Ready service order has no subscription to activate.",
            )
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == order.subscription_id)
            .with_for_update()
        )
        if subscription is None or subscription.subscriber_id != order.subscriber_id:
            raise _error(
                "subscription_scope_mismatch",
                "Subscription does not belong to the service-order subscriber.",
            )
        if subscription.status not in {
            SubscriptionStatus.pending,
            SubscriptionStatus.active,
        }:
            raise _error(
                "subscription_not_activatable",
                "Subscription is not pending or active.",
                status=subscription.status.value,
            )

        if order.status != ServiceOrderStatus.provisioning:
            raise _error(
                "invalid_order_state",
                "Activation can only be requested for an in-flight service order.",
                status=order.status.value,
            )
        decision = _append_decision(
            db,
            order=order,
            run_id=run.id,
            context=command.context,
            status=ProvisioningReadinessDecisionStatus.activation_requested,
            reason_code="activation_projection_requested",
            checks=checks,
        )
        emit_event(
            db,
            EventType.service_order_activation_requested,
            {
                "subscription_id": str(subscription.id),
                "service_order_id": str(order.id),
                "readiness_decision_id": str(decision.id),
                "subscription_status": subscription.status.value,
            },
            actor=command.context.actor,
            subscriber_id=order.subscriber_id,
            subscription_id=subscription.id,
            service_order_id=order.id,
        )
        return _to_outcome(decision)

    return execute_owner_command(
        db,
        definition=_EVALUATE_COMMAND,
        context=command.context,
        operation=operation,
    )


def confirm_activation(
    db: Session, command: ConfirmActivationCommand
) -> ReadinessOutcome:
    """Confirm the exact order after activation projections have succeeded."""

    def operation() -> ReadinessOutcome:
        replay = _decision_for_command(db, command.context.command_id)
        if replay is not None:
            _validate_replay_scope(
                replay,
                service_order_id=command.service_order_id,
            )
            return _to_outcome(replay)
        order = db.scalar(
            select(ServiceOrder)
            .where(ServiceOrder.id == command.service_order_id)
            .with_for_update()
        )
        if order is None:
            raise _error("service_order_not_found", "Service order was not found.")
        if order.subscription_id != command.subscription_id:
            raise _error(
                "subscription_scope_mismatch",
                "Activation confirmation does not match the service order.",
            )
        latest = db.scalar(
            select(ProvisioningReadinessDecision)
            .options(selectinload(ProvisioningReadinessDecision.checks))
            .where(
                ProvisioningReadinessDecision.service_order_id == order.id,
                ProvisioningReadinessDecision.status
                == ProvisioningReadinessDecisionStatus.activation_requested,
            )
            .order_by(ProvisioningReadinessDecision.decided_at.desc())
            .limit(1)
        )
        if latest is None:
            raise _error(
                "activation_not_requested",
                "No readiness decision requested this activation.",
            )
        subscription = db.get(Subscription, command.subscription_id)
        if subscription is None or subscription.status not in {
            SubscriptionStatus.pending,
            SubscriptionStatus.active,
        }:
            raise _error(
                "activation_projection_incomplete",
                "Subscription activation has not been persisted.",
            )
        if order.status == ServiceOrderStatus.active:
            raise _error(
                "invalid_order_state",
                "Service order is already active without this confirmation.",
            )
        if order.status in {ServiceOrderStatus.canceled, ServiceOrderStatus.failed}:
            raise _error(
                "invalid_order_state",
                "Service order cannot be activated in its current state.",
                status=order.status.value,
            )

        checks = tuple(
            ReadinessCheck(
                kind=item.kind,
                result=item.result,
                reason_code=item.reason_code,
                source_type=item.source_type,
                source_id=item.source_id,
            )
            for item in latest.checks
        )
        decision = _append_decision(
            db,
            order=order,
            run_id=latest.provisioning_run_id,
            context=command.context,
            status=ProvisioningReadinessDecisionStatus.activated,
            reason_code="activation_projection_confirmed",
            checks=checks,
        )
        service_order_lifecycle.record_provisioning_result(
            db,
            service_order_id=order.id,
            succeeded=True,
            readiness_decision_id=decision.id,
            actor_id=command.context.actor,
            reason="activation_projection_confirmed",
        )
        emit_event(
            db,
            EventType.subscription_activated,
            {
                "subscription_id": str(subscription.id),
                "service_order_id": str(order.id),
                "readiness_decision_id": str(decision.id),
                "projections_confirmed": True,
                "from_status": SubscriptionStatus.pending.value,
                "to_status": SubscriptionStatus.active.value,
            },
            actor=command.context.actor,
            subscriber_id=order.subscriber_id,
            subscription_id=subscription.id,
            service_order_id=order.id,
        )
        return _to_outcome(decision)

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=command.context,
        operation=operation,
    )
