"""Automation event planning, condition evaluation, and durable run ledger."""

from __future__ import annotations

import hashlib
import json
import logging
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.automation import (
    AutomationRule,
    AutomationRuleStatus,
    AutomationRuleVersion,
    AutomationRun,
    AutomationRunRetry,
    AutomationRunRetryStatus,
    AutomationRunStatus,
    AutomationStepRun,
    AutomationStepStatus,
)
from app.services import automation_actions, automation_capabilities
from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationOperator,
    AutomationValueType,
)
from app.services.domain_errors import DomainError
from app.services.event_replay_evidence import DurableEventReplayEvidence
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "automation.execution"
logger = logging.getLogger(__name__)
RUN_READ_PERMISSION = "automation:run:read"
RUN_REDRIVE_PERMISSION = "automation:run:redrive"
RUN_HISTORY_LIST = ListDefinition(
    key="automation-runs",
    fields=(
        ListFieldDefinition("status", label="Status", filterable=True),
        ListFieldDefinition("created_at", label="Started", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
    default_per_page=50,
    per_page_options=(25, 50, 100),
)
_STEP_LEASE = timedelta(minutes=5)
_PREPARE = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="prepare_automation_event_runs",
)
_CLAIM_STEP = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="claim_automation_step",
)
_FINISH_STEP = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="finish_automation_step",
)
_START_RETRY = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="start_automation_run_retry",
)
_FINISH_RETRY = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation execution decisions and run evidence",
    name="finish_automation_run_retry",
)


class AutomationExecutionError(DomainError):
    pass


class StepClaimDisposition(StrEnum):
    execute = "execute"
    already_succeeded = "already_succeeded"
    busy = "busy"


@dataclass(frozen=True, slots=True)
class AutomationEventEnvelope:
    event_id: UUID
    event_type: str
    trigger_key: str
    tenant_id: UUID
    target_type: str
    target_id: UUID
    occurred_at: datetime
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PrepareAutomationEventCommand:
    event: AutomationEventEnvelope
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PreparedAutomationStep:
    step_id: UUID
    step_index: int
    action_key: str
    inputs: tuple[automation_actions.AutomationActionInputValue, ...]


@dataclass(frozen=True, slots=True)
class PreparedAutomationRun:
    run_id: UUID
    rule_id: UUID
    rule_version_id: UUID
    event_id: UUID
    target: automation_actions.AutomationTargetReference
    steps: tuple[PreparedAutomationStep, ...]


@dataclass(frozen=True, slots=True)
class ClaimAutomationStepCommand:
    tenant_id: UUID
    step_id: UUID
    context: CommandContext


@dataclass(frozen=True, slots=True)
class StepClaimOutcome:
    disposition: StepClaimDisposition
    attempt_count: int


@dataclass(frozen=True, slots=True)
class FinishAutomationStepCommand:
    tenant_id: UUID
    step_id: UUID
    succeeded: bool
    error_code: str | None
    error_message: str | None
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationStepOutcome:
    run_id: UUID
    step_id: UUID
    step_status: AutomationStepStatus
    run_status: AutomationRunStatus


@dataclass(frozen=True, slots=True)
class ListAutomationRunsQuery:
    tenant_id: UUID
    limit: int = 100
    status: AutomationRunStatus | None = None


@dataclass(frozen=True, slots=True)
class AutomationRunSummary:
    run_id: UUID
    rule_id: UUID
    rule_version_id: UUID
    event_id: UUID
    event_type: str
    target_type: str
    target_id: UUID
    status: AutomationRunStatus
    matched: bool | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AutomationStepSummary:
    step_id: UUID
    step_index: int
    action_key: str
    status: AutomationStepStatus
    attempt_count: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AutomationRetrySummary:
    retry_id: UUID
    attempt_number: int
    actor: str
    status: AutomationRunRetryStatus
    error_code: str | None
    error_message: str | None
    resulting_run_status: AutomationRunStatus | None
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class GetAutomationRunDetailQuery:
    tenant_id: UUID
    run_id: UUID


@dataclass(frozen=True, slots=True)
class GetAutomationRunHistoryQuery:
    tenant_id: UUID
    status: AutomationRunStatus | None = None
    page: int = 1
    per_page: int = RUN_HISTORY_LIST.default_per_page
    sort_by: str | None = None
    sort_dir: str | None = None


@dataclass(frozen=True, slots=True)
class AutomationRunHistoryPage:
    list_query: ListQuery
    page: PageMeta
    runs: tuple[AutomationRunSummary, ...]
    previous_url: str | None
    next_url: str | None


@dataclass(frozen=True, slots=True)
class AutomationRunDetail:
    summary: AutomationRunSummary
    rule_name: str
    trigger_key: str
    steps: tuple[AutomationStepSummary, ...]
    retries: tuple[AutomationRetrySummary, ...]


@dataclass(frozen=True, slots=True)
class StartAutomationRunRetryCommand:
    tenant_id: UUID
    run_id: UUID
    event: DurableEventReplayEvidence
    context: CommandContext


@dataclass(frozen=True, slots=True)
class StartedAutomationRunRetry:
    retry_id: UUID
    prepared_run: PreparedAutomationRun | None


@dataclass(frozen=True, slots=True)
class FinishAutomationRunRetryCommand:
    tenant_id: UUID
    retry_id: UUID
    succeeded: bool
    error_code: str | None
    error_message: str | None
    preserve_running_run: bool
    context: CommandContext


@dataclass(frozen=True, slots=True)
class ExecutePreparedAutomationRunCommand:
    tenant_id: UUID
    run: PreparedAutomationRun
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationRunExecutionOutcome:
    run_id: UUID
    status: AutomationRunStatus
    error_code: str | None
    error_message: str | None
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class RetryFailedAutomationRunCommand:
    tenant_id: UUID
    run_id: UUID
    event: DurableEventReplayEvidence
    context: CommandContext


@dataclass(frozen=True, slots=True)
class RetryFailedAutomationRunOutcome:
    retry_id: UUID
    run_id: UUID
    run_status: AutomationRunStatus
    retry_status: AutomationRunRetryStatus
    error_message: str | None


def _error(code: str, message: str, **details: object) -> AutomationExecutionError:
    return AutomationExecutionError(
        code=f"{OWNER}.{code}", message=message, details=details
    )


def _payload_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _path_value(payload: Mapping[str, object], path: str) -> object:
    value: object = payload
    for component in path.split("."):
        if not isinstance(value, Mapping) or component not in value:
            return None
        value = value[component]
    return value


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation
    return Decimal(str(value))


def _typed_value(field: AutomationConditionField, value: object) -> object:
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return tuple(_typed_value(field, item) for item in value)
    if field.value_type in {AutomationValueType.string, AutomationValueType.enum}:
        return str(value)
    if field.value_type is AutomationValueType.integer:
        if isinstance(value, bool):
            raise ValueError
        return int(str(value))
    if field.value_type is AutomationValueType.decimal:
        return _decimal(value)
    if field.value_type is AutomationValueType.boolean:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
        raise ValueError
    if field.value_type is AutomationValueType.date:
        return date.fromisoformat(str(value))
    if field.value_type is AutomationValueType.datetime:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    if field.value_type is AutomationValueType.uuid:
        return UUID(str(value))
    raise ValueError


def _stored_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _stored_mapping_list(
    value: object,
) -> tuple[Mapping[str, object], ...] | None:
    if not isinstance(value, list):
        return None
    items: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        items.append(item)
    return tuple(items)


_ORDERED_COMPARISONS: dict[AutomationOperator, Callable[[object, object], bool]] = {
    AutomationOperator.greater_than: cast(
        Callable[[object, object], bool], operator.gt
    ),
    AutomationOperator.greater_than_or_equal: cast(
        Callable[[object, object], bool], operator.ge
    ),
    AutomationOperator.less_than: cast(Callable[[object, object], bool], operator.lt),
    AutomationOperator.less_than_or_equal: cast(
        Callable[[object, object], bool], operator.le
    ),
}


def _matches_condition(
    *,
    field: AutomationConditionField,
    condition: Mapping[str, object],
    observed: object,
) -> bool:
    try:
        operator = AutomationOperator(str(condition.get("operator") or ""))
        if operator not in field.operators:
            return False
        left = _typed_value(field, observed)
        right = _typed_value(field, condition.get("value"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if operator is AutomationOperator.is_empty:
        return left is None or left == "" or left == ()
    if operator is AutomationOperator.is_not_empty:
        return left is not None and left != "" and left != ()
    if operator is AutomationOperator.equals:
        return left == right
    if operator is AutomationOperator.not_equals:
        return left != right
    if operator is AutomationOperator.in_values:
        return isinstance(right, tuple) and left in right
    if operator is AutomationOperator.not_in_values:
        return isinstance(right, tuple) and left not in right
    if left is None or right is None:
        return False
    comparison = _ORDERED_COMPARISONS.get(operator)
    if comparison is not None:
        try:
            return comparison(left, right)
        except TypeError:
            return False
    if operator is AutomationOperator.contains:
        return str(right) in str(left)
    return False


def _rule_matches(
    *,
    trigger_key: str,
    conditions: list[dict[str, object]],
    payload: Mapping[str, object],
) -> bool:
    trigger = automation_capabilities.trigger_capability(trigger_key)
    fields = {field.key: field for field in trigger.fields}
    for condition in conditions:
        field_key = str(condition.get("field_key") or "")
        field = fields.get(field_key)
        if field is None or not _matches_condition(
            field=field,
            condition=condition,
            observed=_path_value(payload, field_key),
        ):
            return False
    return True


def _prepared_steps(
    db: Session, *, run: AutomationRun, version: AutomationRuleVersion
) -> tuple[PreparedAutomationStep, ...]:
    rows = tuple(
        db.scalars(
            select(AutomationStepRun)
            .where(AutomationStepRun.run_id == run.id)
            .order_by(AutomationStepRun.step_index)
        )
    )
    definitions: dict[int, Mapping[str, object]] = {}
    for item in version.actions:
        position = _stored_int(item.get("position"))
        if position is None or position in definitions:
            raise _error(
                "stored_action_invalid",
                "A stored action has an invalid position.",
            )
        definitions[position] = item
    prepared: list[PreparedAutomationStep] = []
    for row in rows:
        if row.status == AutomationStepStatus.succeeded.value:
            continue
        definition = definitions.get(row.step_index)
        inputs = (
            _stored_mapping_list(definition.get("inputs"))
            if definition is not None
            else None
        )
        if definition is None or inputs is None:
            raise _error(
                "stored_action_invalid",
                "A stored action no longer matches its execution step.",
                step_index=row.step_index,
            )
        prepared.append(
            PreparedAutomationStep(
                step_id=row.id,
                step_index=row.step_index,
                action_key=row.action_key,
                inputs=tuple(
                    automation_actions.AutomationActionInputValue(
                        key=str(item.get("key") or ""), value=item.get("value")
                    )
                    for item in inputs
                ),
            )
        )
    return tuple(prepared)


def prepare_event_runs(
    db: Session, command: PrepareAutomationEventCommand
) -> tuple[PreparedAutomationRun, ...]:
    def operation() -> tuple[PreparedAutomationRun, ...]:
        automation_actions.require_valid_runtime_registry()
        trigger = automation_capabilities.trigger_capability(command.event.trigger_key)
        if trigger.event_type != command.event.event_type:
            raise _error(
                "trigger_event_mismatch",
                "The event does not match the declared trigger.",
            )
        if trigger.entity_type != command.event.target_type:
            raise _error(
                "trigger_target_mismatch",
                "The event target does not match the declared trigger.",
            )
        statement = (
            select(AutomationRule, AutomationRuleVersion)
            .join(
                AutomationRuleVersion,
                AutomationRuleVersion.id == AutomationRule.active_version_id,
            )
            .where(
                AutomationRule.tenant_id == command.event.tenant_id,
                AutomationRule.trigger_key == command.event.trigger_key,
                AutomationRule.status == AutomationRuleStatus.published.value,
            )
            .order_by(AutomationRule.id)
        )
        prepared: list[PreparedAutomationRun] = []
        for rule, version in db.execute(statement).tuples():
            existing = db.scalar(
                select(AutomationRun)
                .where(
                    AutomationRun.rule_version_id == version.id,
                    AutomationRun.event_id == command.event.event_id,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.status in {
                    AutomationRunStatus.succeeded.value,
                    AutomationRunStatus.skipped.value,
                }:
                    continue
                existing.status = AutomationRunStatus.running.value
                existing.error_code = None
                existing.error_message = None
                existing.completed_at = None
                prepared.append(
                    PreparedAutomationRun(
                        run_id=existing.id,
                        rule_id=rule.id,
                        rule_version_id=version.id,
                        event_id=command.event.event_id,
                        target=automation_actions.AutomationTargetReference(
                            entity_type=command.event.target_type,
                            entity_id=command.event.target_id,
                        ),
                        steps=_prepared_steps(db, run=existing, version=version),
                    )
                )
                continue
            matched = _rule_matches(
                trigger_key=rule.trigger_key,
                conditions=version.conditions,
                payload=command.event.payload,
            )
            now = datetime.now(UTC)
            run = AutomationRun(
                tenant_id=command.event.tenant_id,
                rule_id=rule.id,
                rule_version_id=version.id,
                event_id=command.event.event_id,
                event_type=command.event.event_type,
                target_type=command.event.target_type,
                target_id=command.event.target_id,
                status=(
                    AutomationRunStatus.running.value
                    if matched
                    else AutomationRunStatus.skipped.value
                ),
                matched=matched,
                payload_sha256=_payload_sha256(command.event.payload),
                started_at=now,
                completed_at=None if matched else now,
            )
            db.add(run)
            db.flush()
            if not matched:
                continue
            for step in version.actions:
                position = _stored_int(step.get("position"))
                if position is None:
                    raise _error(
                        "stored_action_invalid",
                        "A stored action has an invalid position.",
                    )
                db.add(
                    AutomationStepRun(
                        tenant_id=command.event.tenant_id,
                        run_id=run.id,
                        step_index=position,
                        action_key=str(step["action_key"]),
                        status=AutomationStepStatus.pending.value,
                        idempotency_key=(
                            f"automation:{command.event.event_id}:{version.id}:"
                            f"{position}"
                        ),
                    )
                )
            db.flush()
            prepared.append(
                PreparedAutomationRun(
                    run_id=run.id,
                    rule_id=rule.id,
                    rule_version_id=version.id,
                    event_id=command.event.event_id,
                    target=automation_actions.AutomationTargetReference(
                        entity_type=command.event.target_type,
                        entity_id=command.event.target_id,
                    ),
                    steps=_prepared_steps(db, run=run, version=version),
                )
            )
        return tuple(prepared)

    return execute_owner_command(
        db, definition=_PREPARE, context=command.context, operation=operation
    )


def claim_step(db: Session, command: ClaimAutomationStepCommand) -> StepClaimOutcome:
    def operation() -> StepClaimOutcome:
        step = db.scalar(
            select(AutomationStepRun)
            .where(
                AutomationStepRun.id == command.step_id,
                AutomationStepRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if step is None:
            raise _error("step_not_found", "Automation step not found.")
        if step.status == AutomationStepStatus.succeeded.value:
            return StepClaimOutcome(
                disposition=StepClaimDisposition.already_succeeded,
                attempt_count=step.attempt_count,
            )
        now = datetime.now(UTC)
        if (
            step.status == AutomationStepStatus.running.value
            and step.started_at is not None
            and step.started_at > now - _STEP_LEASE
        ):
            return StepClaimOutcome(
                disposition=StepClaimDisposition.busy,
                attempt_count=step.attempt_count,
            )
        step.status = AutomationStepStatus.running.value
        step.attempt_count += 1
        step.started_at = now
        step.completed_at = None
        step.error_code = None
        step.error_message = None
        db.flush()
        return StepClaimOutcome(
            disposition=StepClaimDisposition.execute,
            attempt_count=step.attempt_count,
        )

    return execute_owner_command(
        db, definition=_CLAIM_STEP, context=command.context, operation=operation
    )


def finish_step(
    db: Session, command: FinishAutomationStepCommand
) -> AutomationStepOutcome:
    def operation() -> AutomationStepOutcome:
        step = db.scalar(
            select(AutomationStepRun)
            .where(
                AutomationStepRun.id == command.step_id,
                AutomationStepRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if step is None:
            raise _error("step_not_found", "Automation step not found.")
        run = db.scalar(
            select(AutomationRun)
            .where(
                AutomationRun.id == step.run_id,
                AutomationRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if run is None:
            raise _error("run_not_found", "Automation run not found.")
        if step.status == AutomationStepStatus.succeeded.value and command.succeeded:
            return AutomationStepOutcome(
                run_id=run.id,
                step_id=step.id,
                step_status=AutomationStepStatus.succeeded,
                run_status=AutomationRunStatus(run.status),
            )
        now = datetime.now(UTC)
        step.status = (
            AutomationStepStatus.succeeded.value
            if command.succeeded
            else AutomationStepStatus.failed.value
        )
        step.error_code = None if command.succeeded else command.error_code
        step.error_message = (
            None
            if command.succeeded
            else (command.error_message or "The automation action failed.")[:1000]
        )
        step.completed_at = now
        if not command.succeeded:
            run.status = AutomationRunStatus.failed.value
            run.error_code = command.error_code or "action_failed"
            run.error_message = (
                command.error_message or "The automation action failed."
            )[:1000]
            run.completed_at = now
            future_steps = tuple(
                db.scalars(
                    select(AutomationStepRun).where(
                        AutomationStepRun.run_id == run.id,
                        AutomationStepRun.step_index > step.step_index,
                        AutomationStepRun.status
                        != AutomationStepStatus.succeeded.value,
                    )
                )
            )
            for future in future_steps:
                future.status = AutomationStepStatus.blocked.value
                future.error_code = "automation.execution.blocked_after_failure"
                future.error_message = (
                    "This step did not run because an earlier action failed."
                )
        else:
            remaining = int(
                db.scalar(
                    select(func.count(AutomationStepRun.id)).where(
                        AutomationStepRun.run_id == run.id,
                        AutomationStepRun.id != step.id,
                        AutomationStepRun.status
                        != AutomationStepStatus.succeeded.value,
                    )
                )
                or 0
            )
            if remaining == 0:
                run.status = AutomationRunStatus.succeeded.value
                run.error_code = None
                run.error_message = None
                run.completed_at = now
            else:
                run.status = AutomationRunStatus.running.value
                run.completed_at = None
        db.flush()
        return AutomationStepOutcome(
            run_id=run.id,
            step_id=step.id,
            step_status=AutomationStepStatus(step.status),
            run_status=AutomationRunStatus(run.status),
        )

    return execute_owner_command(
        db, definition=_FINISH_STEP, context=command.context, operation=operation
    )


def list_runs(
    db: Session, query: ListAutomationRunsQuery
) -> tuple[AutomationRunSummary, ...]:
    limit = min(max(query.limit, 1), 500)
    statement = select(AutomationRun).where(AutomationRun.tenant_id == query.tenant_id)
    if query.status is not None:
        statement = statement.where(AutomationRun.status == query.status.value)
    rows = tuple(
        db.scalars(
            statement.order_by(AutomationRun.created_at.desc(), AutomationRun.id).limit(
                limit
            )
        )
    )
    return tuple(
        AutomationRunSummary(
            run_id=row.id,
            rule_id=row.rule_id,
            rule_version_id=row.rule_version_id,
            event_id=row.event_id,
            event_type=row.event_type,
            target_type=row.target_type,
            target_id=row.target_id,
            status=AutomationRunStatus(row.status),
            matched=row.matched,
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    )


def list_run_history(
    db: Session, query: GetAutomationRunHistoryQuery
) -> AutomationRunHistoryPage:
    try:
        list_query = RUN_HISTORY_LIST.build_query(
            search=None,
            filters={"status": query.status.value if query.status else None},
            sort_by=query.sort_by,
            sort_dir=query.sort_dir,
            page=query.page,
            per_page=query.per_page,
        )
    except ValueError as exc:
        raise _error(
            "run_history_query_invalid",
            "The run history filter or page size is invalid.",
        ) from exc
    status = query.status
    count_statement = select(func.count(AutomationRun.id)).where(
        AutomationRun.tenant_id == query.tenant_id
    )
    if status is not None:
        count_statement = count_statement.where(AutomationRun.status == status.value)
    total = int(db.scalar(count_statement) or 0)
    page_meta = PageMeta.from_query(list_query, total)
    normalized_query = list_query.with_page(page_meta.page)
    statement = select(AutomationRun).where(AutomationRun.tenant_id == query.tenant_id)
    if status is not None:
        statement = statement.where(AutomationRun.status == status.value)
    ordering = (
        AutomationRun.created_at.asc()
        if normalized_query.sort_dir == "asc"
        else AutomationRun.created_at.desc()
    )
    rows = tuple(
        db.scalars(
            statement.order_by(ordering, AutomationRun.id.desc())
            .offset(normalized_query.offset)
            .limit(normalized_query.per_page)
        )
    )
    return AutomationRunHistoryPage(
        list_query=normalized_query,
        page=page_meta,
        runs=tuple(
            AutomationRunSummary(
                run_id=row.id,
                rule_id=row.rule_id,
                rule_version_id=row.rule_version_id,
                event_id=row.event_id,
                event_type=row.event_type,
                target_type=row.target_type,
                target_id=row.target_id,
                status=AutomationRunStatus(row.status),
                matched=row.matched,
                error_code=row.error_code,
                error_message=row.error_message,
                created_at=row.created_at,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
            for row in rows
        ),
        previous_url=(
            normalized_query.url("/admin/automation/runs", page=page_meta.page - 1)
            if page_meta.has_previous
            else None
        ),
        next_url=(
            normalized_query.url("/admin/automation/runs", page=page_meta.page + 1)
            if page_meta.has_next
            else None
        ),
    )


def get_run_detail(
    db: Session, query: GetAutomationRunDetailQuery
) -> AutomationRunDetail:
    run = db.scalar(
        select(AutomationRun).where(
            AutomationRun.id == query.run_id,
            AutomationRun.tenant_id == query.tenant_id,
        )
    )
    if run is None:
        raise _error("run_not_found", "Automation run not found.")
    rule = db.scalar(
        select(AutomationRule).where(
            AutomationRule.id == run.rule_id,
            AutomationRule.tenant_id == query.tenant_id,
        )
    )
    if rule is None:
        raise _error("run_rule_not_found", "The rule for this run is unavailable.")
    step_rows = tuple(
        db.scalars(
            select(AutomationStepRun)
            .where(
                AutomationStepRun.run_id == run.id,
                AutomationStepRun.tenant_id == query.tenant_id,
            )
            .order_by(AutomationStepRun.step_index)
        )
    )
    retry_rows = tuple(
        db.scalars(
            select(AutomationRunRetry)
            .where(
                AutomationRunRetry.run_id == run.id,
                AutomationRunRetry.tenant_id == query.tenant_id,
            )
            .order_by(AutomationRunRetry.attempt_number.desc())
        )
    )
    summary = AutomationRunSummary(
        run_id=run.id,
        rule_id=run.rule_id,
        rule_version_id=run.rule_version_id,
        event_id=run.event_id,
        event_type=run.event_type,
        target_type=run.target_type,
        target_id=run.target_id,
        status=AutomationRunStatus(run.status),
        matched=run.matched,
        error_code=run.error_code,
        error_message=run.error_message,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )
    return AutomationRunDetail(
        summary=summary,
        rule_name=rule.name,
        trigger_key=rule.trigger_key,
        steps=tuple(
            AutomationStepSummary(
                step_id=row.id,
                step_index=row.step_index,
                action_key=row.action_key,
                status=AutomationStepStatus(row.status),
                attempt_count=row.attempt_count,
                error_code=row.error_code,
                error_message=row.error_message,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
            for row in step_rows
        ),
        retries=tuple(
            AutomationRetrySummary(
                retry_id=row.id,
                attempt_number=row.attempt_number,
                actor=row.actor,
                status=AutomationRunRetryStatus(row.status),
                error_code=row.error_code,
                error_message=row.error_message,
                resulting_run_status=(
                    AutomationRunStatus(row.resulting_run_status)
                    if row.resulting_run_status
                    else None
                ),
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
            for row in retry_rows
        ),
    )


def start_run_retry(
    db: Session, command: StartAutomationRunRetryCommand
) -> StartedAutomationRunRetry:
    def operation() -> StartedAutomationRunRetry:
        duplicate = db.scalar(
            select(AutomationRunRetry).where(
                AutomationRunRetry.command_id == command.context.command_id
            )
        )
        if duplicate is not None:
            if (
                duplicate.run_id != command.run_id
                or duplicate.tenant_id != command.tenant_id
            ):
                raise _error(
                    "retry_command_conflict",
                    "This retry request was already used for another run.",
                )
            return StartedAutomationRunRetry(retry_id=duplicate.id, prepared_run=None)
        run = db.scalar(
            select(AutomationRun)
            .where(
                AutomationRun.id == command.run_id,
                AutomationRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if run is None:
            raise _error("run_not_found", "Automation run not found.")
        if run.status != AutomationRunStatus.failed.value:
            raise _error(
                "run_not_retryable",
                "Only a failed automation run can be retried.",
                status=run.status,
            )
        if (
            command.event.event_id != run.event_id
            or command.event.event_type.value != run.event_type
        ):
            raise _error(
                "retry_event_mismatch",
                "The original event does not match this failed run.",
            )
        rule = db.scalar(
            select(AutomationRule).where(
                AutomationRule.id == run.rule_id,
                AutomationRule.tenant_id == command.tenant_id,
            )
        )
        if rule is None:
            raise _error("run_rule_not_found", "The rule for this run is unavailable.")
        try:
            trigger = automation_capabilities.trigger_capability(rule.trigger_key)
        except automation_capabilities.AutomationCapabilityError as exc:
            raise _error(
                "retry_trigger_unavailable",
                "The trigger for this run is no longer available.",
            ) from exc
        if (
            trigger.event_type != command.event.event_type.value
            or trigger.entity_type != run.target_type
            or not trigger.runtime_enabled
        ):
            raise _error(
                "retry_trigger_mismatch",
                "The original event no longer matches this run.",
            )
        try:
            event_tenant = UUID(
                str(_path_value(command.event.payload, trigger.tenant_id_field))
            )
            target_id = UUID(
                str(_path_value(command.event.payload, trigger.entity_id_field))
            )
        except (TypeError, ValueError) as exc:
            raise _error(
                "retry_event_identity_invalid",
                "The original event is missing its customer or affected record.",
            ) from exc
        if event_tenant != command.tenant_id or target_id != run.target_id:
            raise _error(
                "retry_target_mismatch",
                "The original event no longer matches the affected record.",
            )
        version = db.scalar(
            select(AutomationRuleVersion).where(
                AutomationRuleVersion.id == run.rule_version_id,
                AutomationRuleVersion.tenant_id == command.tenant_id,
            )
        )
        if version is None:
            raise _error(
                "run_version_unavailable",
                "The rule version for this run is no longer available.",
            )
        try:
            automation_actions.require_valid_runtime_registry()
        except automation_actions.AutomationActionExecutorError as exc:
            raise _error(
                "retry_runtime_unavailable",
                "An action needed by this run is no longer available.",
            ) from exc
        steps = _prepared_steps(db, run=run, version=version)
        if not steps:
            raise _error(
                "run_has_no_retryable_steps",
                "This failed run has no unfinished steps to continue.",
            )
        try:
            for step in steps:
                capability = automation_capabilities.action_capability(step.action_key)
                if not capability.runtime_enabled:
                    raise automation_actions.AutomationActionExecutorError(
                        f"Automation action {step.action_key!r} is disabled."
                    )
                automation_actions.action_executor(step.action_key)
        except (
            automation_capabilities.AutomationCapabilityError,
            automation_actions.AutomationActionExecutorError,
        ) as exc:
            raise _error(
                "retry_runtime_unavailable",
                "An action needed by this run is no longer available.",
            ) from exc
        attempt_number = (
            int(
                db.scalar(
                    select(
                        func.coalesce(func.max(AutomationRunRetry.attempt_number), 0)
                    ).where(AutomationRunRetry.run_id == run.id)
                )
                or 0
            )
            + 1
        )
        now = datetime.now(UTC)
        retry = AutomationRunRetry(
            tenant_id=command.tenant_id,
            run_id=run.id,
            attempt_number=attempt_number,
            command_id=command.context.command_id,
            actor=command.context.actor[:255],
            status=AutomationRunRetryStatus.running.value,
            started_at=now,
        )
        run.status = AutomationRunStatus.running.value
        run.error_code = None
        run.error_message = None
        run.completed_at = None
        db.add(retry)
        db.flush()
        return StartedAutomationRunRetry(
            retry_id=retry.id,
            prepared_run=PreparedAutomationRun(
                run_id=run.id,
                rule_id=run.rule_id,
                rule_version_id=version.id,
                event_id=run.event_id,
                target=automation_actions.AutomationTargetReference(
                    entity_type=run.target_type,
                    entity_id=run.target_id,
                ),
                steps=steps,
            ),
        )

    return execute_owner_command(
        db,
        definition=_START_RETRY,
        context=command.context,
        operation=operation,
    )


def finish_run_retry(
    db: Session, command: FinishAutomationRunRetryCommand
) -> AutomationRunStatus:
    def operation() -> AutomationRunStatus:
        retry = db.scalar(
            select(AutomationRunRetry)
            .where(
                AutomationRunRetry.id == command.retry_id,
                AutomationRunRetry.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if retry is None:
            raise _error("retry_not_found", "Automation retry record not found.")
        run = db.scalar(
            select(AutomationRun)
            .where(
                AutomationRun.id == retry.run_id,
                AutomationRun.tenant_id == command.tenant_id,
            )
            .with_for_update()
        )
        if run is None:
            raise _error("run_not_found", "Automation run not found.")
        now = datetime.now(UTC)
        if (
            not command.succeeded
            and not command.preserve_running_run
            and run.status != AutomationRunStatus.failed.value
        ):
            run.status = AutomationRunStatus.failed.value
            run.error_code = command.error_code or "retry_failed"
            run.error_message = (
                command.error_message or "The retry could not complete."
            )[:1000]
            run.completed_at = now
        retry.status = (
            AutomationRunRetryStatus.succeeded.value
            if command.succeeded
            else AutomationRunRetryStatus.failed.value
        )
        retry.error_code = None if command.succeeded else command.error_code
        retry.error_message = (
            None
            if command.succeeded
            else (command.error_message or "The retry could not complete.")[:1000]
        )
        retry.resulting_run_status = run.status
        retry.completed_at = now
        db.flush()
        return AutomationRunStatus(run.status)

    return execute_owner_command(
        db,
        definition=_FINISH_RETRY,
        context=command.context,
        operation=operation,
    )


def execute_prepared_run(
    db: Session, command: ExecutePreparedAutomationRunCommand
) -> AutomationRunExecutionOutcome:
    """Execute only prepared unfinished steps through their declared owners."""

    for step in command.run.steps:
        step_key = f"{command.run.rule_version_id}:{step.step_index}"
        claim = claim_step(
            db,
            ClaimAutomationStepCommand(
                tenant_id=command.tenant_id,
                step_id=step.step_id,
                context=CommandContext.system(
                    actor="automation-runtime",
                    scope="automation:runtime",
                    reason=f"Claim automation action step {step_key}",
                    command_id=uuid4(),
                    correlation_id=command.run.event_id,
                    causation_id=command.context.command_id,
                    idempotency_key=(
                        f"automation-claim:{command.run.event_id}:{step_key}"
                    ),
                ),
            ),
        )
        if claim.disposition is StepClaimDisposition.already_succeeded:
            continue
        if claim.disposition is StepClaimDisposition.busy:
            return AutomationRunExecutionOutcome(
                run_id=command.run.run_id,
                status=AutomationRunStatus.running,
                error_code="automation.execution.step_busy",
                error_message=(
                    "Another worker is already processing this action step."
                ),
                retryable=True,
            )
        action = automation_capabilities.action_capability(step.action_key)
        executor = automation_actions.action_executor(step.action_key)
        idempotency_key = (
            f"automation:{command.run.event_id}:"
            f"{command.run.rule_version_id}:{step.step_index}"
        )
        succeeded = False
        error_code: str | None = None
        error_message: str | None = None
        retryable = True
        try:
            executor(
                db,
                automation_actions.ExecuteAutomationActionCommand(
                    tenant_id=command.tenant_id,
                    event_id=command.run.event_id,
                    rule_id=command.run.rule_id,
                    rule_version_id=command.run.rule_version_id,
                    step_index=step.step_index,
                    target=command.run.target,
                    inputs=step.inputs,
                    context=CommandContext.system(
                        actor="automation-runtime",
                        scope=action.runtime_scope,
                        reason=f"Execute declared action {step.action_key}",
                        command_id=uuid5(NAMESPACE_URL, f"dotmac:{idempotency_key}"),
                        correlation_id=command.run.event_id,
                        causation_id=command.context.command_id,
                        idempotency_key=idempotency_key,
                    ),
                ),
            )
            succeeded = True
        except DomainError as exc:
            error_code = exc.code
            error_message = exc.message
            retryable = exc.retryable
        except Exception:
            error_code = "automation.execution.action_failed"
            error_message = (
                "The action failed unexpectedly. Review the step and try again."
            )
        outcome = finish_step(
            db,
            FinishAutomationStepCommand(
                tenant_id=command.tenant_id,
                step_id=step.step_id,
                succeeded=succeeded,
                error_code=error_code,
                error_message=error_message,
                context=CommandContext.system(
                    actor="automation-runtime",
                    scope="automation:runtime",
                    reason=f"Record automation action step {step_key}",
                    command_id=uuid4(),
                    correlation_id=command.run.event_id,
                    causation_id=command.context.command_id,
                    idempotency_key=(
                        f"automation-finish:{command.run.event_id}:{step_key}"
                    ),
                ),
            ),
        )
        if not succeeded:
            return AutomationRunExecutionOutcome(
                run_id=command.run.run_id,
                status=outcome.run_status,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
            )
    return AutomationRunExecutionOutcome(
        run_id=command.run.run_id,
        status=AutomationRunStatus.succeeded,
        error_code=None,
        error_message=None,
        retryable=True,
    )


def retry_failed_run(
    db: Session, command: RetryFailedAutomationRunCommand
) -> RetryFailedAutomationRunOutcome:
    """Start, continue, and durably audit one administrator run retry."""

    started = start_run_retry(
        db,
        StartAutomationRunRetryCommand(
            tenant_id=command.tenant_id,
            run_id=command.run_id,
            event=command.event,
            context=command.context,
        ),
    )
    if started.prepared_run is None:
        retry = db.scalar(
            select(AutomationRunRetry).where(
                AutomationRunRetry.id == started.retry_id,
                AutomationRunRetry.tenant_id == command.tenant_id,
            )
        )
        run = (
            db.scalar(
                select(AutomationRun).where(
                    AutomationRun.id == command.run_id,
                    AutomationRun.tenant_id == command.tenant_id,
                )
            )
            if retry is not None
            else None
        )
        if retry is None or run is None:
            raise _error("retry_not_found", "Automation retry record not found.")
        return RetryFailedAutomationRunOutcome(
            retry_id=retry.id,
            run_id=run.id,
            run_status=AutomationRunStatus(run.status),
            retry_status=AutomationRunRetryStatus(retry.status),
            error_message=retry.error_message,
        )
    try:
        execution = execute_prepared_run(
            db,
            ExecutePreparedAutomationRunCommand(
                tenant_id=command.tenant_id,
                run=started.prepared_run,
                context=command.context,
            ),
        )
    except DomainError as exc:
        execution = AutomationRunExecutionOutcome(
            run_id=command.run_id,
            status=AutomationRunStatus.failed,
            error_code=exc.code,
            error_message=exc.message,
            retryable=exc.retryable,
        )
    except Exception:
        logger.exception(
            "automation_run_retry_execution_failed",
            extra={"run_id": str(command.run_id), "retry_id": str(started.retry_id)},
        )
        execution = AutomationRunExecutionOutcome(
            run_id=command.run_id,
            status=AutomationRunStatus.failed,
            error_code="automation.execution.retry_failed",
            error_message=(
                "The retry stopped before all steps completed. Review the step details."
            ),
            retryable=True,
        )
    finish_context = CommandContext.system(
        actor=command.context.actor,
        scope=command.context.scope,
        reason="Record the result of an administrator automation retry",
        command_id=uuid5(
            NAMESPACE_URL, f"dotmac:automation:retry:{started.retry_id}:finish"
        ),
        correlation_id=command.run_id,
        causation_id=command.context.command_id,
        idempotency_key=f"automation-run-retry-finish:{started.retry_id}",
    )
    resulting_status = finish_run_retry(
        db,
        FinishAutomationRunRetryCommand(
            tenant_id=command.tenant_id,
            retry_id=started.retry_id,
            succeeded=execution.error_code is None,
            error_code=execution.error_code,
            error_message=execution.error_message,
            preserve_running_run=execution.status is AutomationRunStatus.running,
            context=finish_context,
        ),
    )
    retry_status = (
        AutomationRunRetryStatus.succeeded
        if execution.error_code is None
        else AutomationRunRetryStatus.failed
    )
    return RetryFailedAutomationRunOutcome(
        retry_id=started.retry_id,
        run_id=command.run_id,
        run_status=resulting_status,
        retry_status=retry_status,
        error_message=execution.error_message,
    )
