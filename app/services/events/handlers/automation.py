"""Durable EventStore adapter for the Automation Center runtime."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session

from app.models.automation_scripts import AutomationScriptRunStatus
from app.services import (
    automation_actions,
    automation_capabilities,
    automation_runtime,
    automation_scripts,
)
from app.services.automation_contracts import (
    AutomationActionCapability,
    AutomationScriptTargetCapability,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.automation_script_runner import (
    AutomationScriptActionRequest,
    AutomationScriptRunner,
    ExecuteAutomationServerScriptCommand,
    parse_script_action_requests,
)
from app.services.events.handlers.owner_session import owner_session
from app.services.events.types import Event, EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext


def _registered_triggers() -> tuple[AutomationTriggerCapability, ...]:
    return tuple(
        trigger
        for module in automation_capabilities.registered_module_manifests()
        for trigger in module.triggers
        if trigger.runtime_enabled
    )


def _registered_script_targets() -> tuple[AutomationScriptTargetCapability, ...]:
    return tuple(
        target
        for module in automation_capabilities.registered_module_manifests()
        for target in module.script_targets
        if target.server_events
    )


def _event_type_for_name(event_name: str) -> EventType:
    return (
        EventType.custom
        if event_name not in {item.value for item in EventType}
        else EventType(event_name)
    )


HANDLED_EVENT_TYPES = frozenset(
    _event_type_for_name(event_name)
    for event_name in {
        *(trigger.event_type for trigger in _registered_triggers()),
        *(
            event_name
            for target in _registered_script_targets()
            for event_name in target.server_events
        ),
    }
)


class AutomationEventHandlerError(RuntimeError):
    """Keep the durable event retryable when any automation action fails."""


def _path_value(payload: Mapping[str, object], path: str) -> object:
    value: object = payload
    for component in path.split("."):
        if not isinstance(value, Mapping) or component not in value:
            return None
        value = value[component]
    return value


def _required_uuid(payload: Mapping[str, object], path: str) -> UUID:
    value = _path_value(payload, path)
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise AutomationEventHandlerError(
            f"Automation event identity field {path!r} is missing or invalid."
        ) from exc


def _trigger_matches_event(trigger: AutomationTriggerCapability, event: Event) -> bool:
    """Match normal EventType values and owner-defined custom event names."""

    event_type = trigger.event_type
    if event.event_type.value == event_type:
        return True
    return (
        event.event_type is EventType.custom and event.payload.get("name") == event_type
    )


def _event_name(event: Event) -> str:
    if event.event_type is EventType.custom:
        name = event.payload.get("name")
        if isinstance(name, str) and name:
            return name
    return event.event_type.value


def _script_action_inputs(
    action: AutomationActionCapability,
    request: AutomationScriptActionRequest,
) -> tuple[automation_actions.AutomationActionInputValue, ...]:
    """Validate one script result against the registered action input schema."""

    raw_inputs = tuple(request.inputs)
    if len({item.key for item in raw_inputs}) != len(raw_inputs):
        raise AutomationEventHandlerError(
            f"Server script repeated an input for action {action.key!r}."
        )
    declared = {item.key: item for item in action.inputs}
    supplied = {item.key: item.value for item in raw_inputs}
    unknown = sorted(set(supplied) - set(declared))
    missing = sorted(
        item.key
        for item in action.inputs
        if item.required and (item.key not in supplied or supplied[item.key] is None)
    )
    if unknown or missing:
        raise AutomationEventHandlerError(
            f"Server script inputs for {action.key!r} are invalid "
            f"(unknown={unknown!r}, missing={missing!r})."
        )
    for key, value in supplied.items():
        definition = declared[key]
        valid = True
        if value is None:
            valid = not definition.required
        elif definition.value_type is AutomationValueType.string:
            valid = isinstance(value, str)
        elif definition.value_type is AutomationValueType.integer:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif definition.value_type is AutomationValueType.decimal:
            try:
                Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                valid = False
        elif definition.value_type is AutomationValueType.boolean:
            valid = isinstance(value, bool)
        elif definition.value_type is AutomationValueType.uuid:
            try:
                UUID(str(value))
            except (TypeError, ValueError):
                valid = False
        elif definition.value_type is AutomationValueType.date:
            try:
                date.fromisoformat(str(value))
            except ValueError:
                valid = False
        elif definition.value_type is AutomationValueType.datetime:
            try:
                valid = datetime.fromisoformat(str(value)).tzinfo is not None
            except ValueError:
                valid = False
        elif definition.value_type is AutomationValueType.enum:
            valid = isinstance(value, str) and value in definition.enum_values
        if not valid:
            raise AutomationEventHandlerError(
                f"Server script input {key!r} for {action.key!r} has the wrong type."
            )
    return tuple(
        automation_actions.AutomationActionInputValue(key=item.key, value=item.value)
        for item in raw_inputs
    )


def _context(
    *,
    event: Event,
    operation: str,
    idempotency_key: str | None = None,
    scope: str = "automation:runtime",
) -> CommandContext:
    command_id = uuid5(NAMESPACE_URL, f"dotmac:automation:{event.event_id}:{operation}")
    return CommandContext.system(
        actor="automation-event-handler",
        scope=scope,
        reason=f"Execute automation for durable event {event.event_type.value}",
        command_id=command_id,
        correlation_id=event.event_id,
        causation_id=event.event_id,
        idempotency_key=idempotency_key,
    )


class AutomationEventHandler:
    """Translate registered events into ordered, receipted module commands."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        payload: Mapping[str, object] = event.payload
        triggers = tuple(
            trigger
            for trigger in _registered_triggers()
            if _trigger_matches_event(trigger, event)
        )
        event_name = _event_name(event)
        processed_script_targets: set[tuple[str, str]] = set()
        for trigger in triggers:
            tenant_id = _required_uuid(payload, trigger.tenant_id_field)
            if tenant_id != OPERATOR_TENANT_ID:
                raise AutomationEventHandlerError(
                    "Automation event tenant does not match the operator tenant."
                )
            target_id = _required_uuid(payload, trigger.entity_id_field)
            self._execute_server_scripts(
                db,
                event=event,
                target_type=trigger.entity_type,
                event_name=event_name,
                tenant_id=tenant_id,
                target_id=target_id,
                payload=payload,
            )
            processed_script_targets.add((trigger.entity_type, event_name))
            envelope = automation_runtime.AutomationEventEnvelope(
                event_id=event.event_id,
                event_type=event.event_type.value,
                trigger_key=trigger.key,
                tenant_id=tenant_id,
                target_type=trigger.entity_type,
                target_id=target_id,
                occurred_at=event.occurred_at,
                payload=payload,
            )
            with owner_session(db) as command_db:
                runs = automation_runtime.prepare_event_runs(
                    command_db,
                    automation_runtime.PrepareAutomationEventCommand(
                        event=envelope,
                        context=_context(
                            event=event, operation=f"prepare:{trigger.key}"
                        ),
                    ),
                )
            for run in runs:
                self.execute_prepared_run(db, event=event, run=run, tenant_id=tenant_id)

        for target in _registered_script_targets():
            if event_name not in target.server_events:
                continue
            target_key = (target.entity_type, event_name)
            if target_key in processed_script_targets:
                continue
            tenant_id = _required_uuid(payload, target.tenant_id_field)
            if tenant_id != OPERATOR_TENANT_ID:
                raise AutomationEventHandlerError(
                    "Automation event tenant does not match the operator tenant."
                )
            target_id = _required_uuid(payload, target.entity_id_field)
            self._execute_server_scripts(
                db,
                event=event,
                target_type=target.entity_type,
                event_name=event_name,
                tenant_id=tenant_id,
                target_id=target_id,
                payload=payload,
            )

    def _execute_script_actions(
        self,
        db: Session,
        *,
        event: Event,
        script: automation_scripts.PublishedAutomationServerScript,
        tenant_id: UUID,
        target_id: UUID,
        output: Mapping[str, object],
    ) -> None:
        requests = parse_script_action_requests(output)
        for step_index, request in enumerate(requests):
            try:
                action = automation_capabilities.action_capability(request.action_key)
                if not action.runtime_enabled:
                    raise AutomationEventHandlerError(
                        f"Server script action {action.key!r} is not runtime-enabled."
                    )
                if action.entity_type != script.target_type:
                    raise AutomationEventHandlerError(
                        f"Server script action {action.key!r} targets "
                        f"{action.entity_type!r}, not {script.target_type!r}."
                    )
                inputs = _script_action_inputs(action, request)
                executor = automation_actions.action_executor(action.key)
                action_context = _context(
                    event=event,
                    operation=f"server-script-action:{script.script_id}:{step_index}",
                    scope="automation:script:runtime",
                    idempotency_key=(
                        f"automation-server-script-action:{script.script_id}:"
                        f"{script.version_id}:{event.event_id}:{step_index}"
                    ),
                )
                executor(
                    db,
                    automation_actions.ExecuteAutomationActionCommand(
                        tenant_id=tenant_id,
                        event_id=event.event_id,
                        # The typed action owners retain script/version
                        # provenance in their existing automation evidence
                        # fields. The context scope distinguishes this from a
                        # native rule execution.
                        rule_id=script.script_id,
                        rule_version_id=script.version_id,
                        step_index=step_index,
                        target=automation_actions.AutomationTargetReference(
                            entity_type=script.target_type,
                            entity_id=target_id,
                        ),
                        inputs=inputs,
                        context=action_context,
                    ),
                )
            except automation_capabilities.AutomationCapabilityError as exc:
                raise AutomationEventHandlerError(
                    f"Server script requested an undeclared action {request.action_key!r}."
                ) from exc

    def _execute_server_scripts(
        self,
        db: Session,
        *,
        event: Event,
        target_type: str,
        event_name: str,
        tenant_id: UUID,
        target_id: UUID,
        payload: Mapping[str, object],
    ) -> None:
        scripts = automation_scripts.published_server_scripts(
            db,
            tenant_id=tenant_id,
            target_type=target_type,
            event_name=event_name,
        )
        if not scripts:
            return
        runner = AutomationScriptRunner()
        for script in scripts:
            script_context = _context(
                event=event,
                operation=f"server-script:{script.script_id}",
                idempotency_key=(
                    f"automation-server-script:{script.script_id}:{event.event_id}"
                ),
            )
            with owner_session(db) as command_db:
                admission = automation_scripts.start_script_run(
                    command_db,
                    automation_scripts.StartAutomationScriptRunCommand(
                        tenant_id=tenant_id,
                        script_id=script.script_id,
                        version_id=script.version_id,
                        event_id=event.event_id,
                        target_type=script.target_type,
                        target_id=target_id,
                        context=script_context,
                    ),
                )
            if not admission.should_execute:
                continue
            try:
                result = runner.execute(
                    db,
                    ExecuteAutomationServerScriptCommand(
                        tenant_id=tenant_id,
                        script_id=script.script_id,
                        version_id=script.version_id,
                        event_id=event.event_id,
                        target_type=script.target_type,
                        target_id=target_id,
                        event_name=script.event_name,
                        event_payload=payload,
                        context=script_context,
                    ),
                )
                if result.status.value == "succeeded":
                    self._execute_script_actions(
                        db,
                        event=event,
                        script=script,
                        tenant_id=tenant_id,
                        target_id=target_id,
                        output=result.output,
                    )
                status = (
                    AutomationScriptRunStatus.succeeded
                    if result.status.value == "succeeded"
                    else (
                        AutomationScriptRunStatus.reconciliation_required
                        if result.status.value == "reconciliation_required"
                        else AutomationScriptRunStatus.failed
                    )
                )
                error_code = result.error_code
                result_code = result.status.value
            except Exception as exc:
                status = AutomationScriptRunStatus.failed
                error_code = (
                    getattr(exc, "code", None)
                    or "automation.script_execution.runtime_error"
                )
                result_code = None
            with owner_session(db) as command_db:
                automation_scripts.finish_script_run(
                    command_db,
                    automation_scripts.FinishAutomationScriptRunCommand(
                        tenant_id=tenant_id,
                        run_id=admission.run_id,
                        status=status,
                        result_code=result_code,
                        error_code=error_code,
                        context=script_context,
                    ),
                )
            if status is not AutomationScriptRunStatus.succeeded:
                raise AutomationEventHandlerError(
                    f"Server script {script.script_id} stopped with {status.value}."
                )

    def execute_prepared_run(
        self,
        db: Session,
        *,
        event: Event,
        run: automation_runtime.PreparedAutomationRun,
        tenant_id: UUID,
    ) -> None:
        with owner_session(db) as command_db:
            outcome = automation_runtime.execute_prepared_run(
                command_db,
                automation_runtime.ExecutePreparedAutomationRunCommand(
                    tenant_id=tenant_id,
                    run=run,
                    context=_context(event=event, operation=f"execute:{run.run_id}"),
                ),
            )
        if outcome.error_code:
            raise AutomationEventHandlerError(
                f"Automation run {run.run_id} stopped with {outcome.error_code}."
            )


__all__ = [
    "HANDLED_EVENT_TYPES",
    "AutomationEventHandler",
    "AutomationEventHandlerError",
]
