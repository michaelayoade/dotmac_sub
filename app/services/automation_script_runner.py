"""Out-of-process execution adapter for published Automation Center scripts.

The application owns script identity and publication state. This module only
marshals an approved immutable version to the existing hardened OCI runner;
it never evaluates JavaScript in the web or worker process.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.automation_scripts import (
    AutomationScript,
    AutomationScriptKind,
    AutomationScriptStatus,
    AutomationScriptVersion,
)
from app.services.automation_script_runtime import (
    AutomationScriptRuntimeState,
    current_policy,
    runtime_state,
)
from app.services.domain_errors import DomainError
from app.services.integrations.egress_policy import EgressPolicy
from app.services.integrations.external_runner import ExternalOciRunner, RunnerTransport
from app.services.integrations.manifest import (
    CapabilityManifest,
    CapabilityMode,
    ConnectorManifest,
    ConnectorRuntimeType,
    RuntimeManifest,
)
from app.services.integrations.podman_transport import PodmanTransport
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationResult,
    OperationTrigger,
)
from app.services.owner_commands import CommandContext

_RUNTIME_KEY = "automation-script-runtime"
_RUNTIME_VERSION = "1.0.0"
_CAPABILITY_ID = "automation.server_script.execute.v1"


class AutomationScriptExecutionError(DomainError):
    """A published script could not be admitted to the isolated runtime."""


class AutomationScriptRuntimeUnavailable(AutomationScriptExecutionError):
    """The deployment has not configured a valid script runtime."""


class AutomationScriptActionProtocolError(AutomationScriptExecutionError):
    """A script returned an action request outside the typed result contract."""


@dataclass(frozen=True, slots=True)
class AutomationScriptActionInput:
    """One JSON action input returned by a server script."""

    key: str
    value: object


@dataclass(frozen=True, slots=True)
class AutomationScriptActionRequest:
    """One typed action request returned by a server script."""

    action_key: str
    inputs: tuple[AutomationScriptActionInput, ...]


def parse_script_action_requests(
    output: Mapping[str, object],
) -> tuple[AutomationScriptActionRequest, ...]:
    """Parse the only side-effect contract admitted from a server script.

    A script may return ``{"actions": [{"action_key": ..., "inputs": {...}}]}``.
    The action key and input schema are still validated against the canonical
    Automation Center registry before any owner adapter is called.
    """

    raw_actions = output.get("actions", ())
    if raw_actions in (None, ()):
        return ()
    if not isinstance(raw_actions, (list, tuple)) or len(raw_actions) > 10:
        raise AutomationScriptActionProtocolError(
            code="automation.script_execution.action_contract_invalid",
            message="Server script actions must be a list of at most ten requests.",
            details={},
        )
    requests: list[AutomationScriptActionRequest] = []
    for raw_action in raw_actions:
        if not isinstance(raw_action, Mapping):
            raise AutomationScriptActionProtocolError(
                code="automation.script_execution.action_contract_invalid",
                message="Each server-script action must be an object.",
                details={},
            )
        action_key = raw_action.get("action_key")
        raw_inputs = raw_action.get("inputs", {})
        if not isinstance(action_key, str) or not action_key.strip():
            raise AutomationScriptActionProtocolError(
                code="automation.script_execution.action_contract_invalid",
                message="Each server-script action needs a non-empty action key.",
                details={},
            )
        if not isinstance(raw_inputs, Mapping) or len(raw_inputs) > 20:
            raise AutomationScriptActionProtocolError(
                code="automation.script_execution.action_contract_invalid",
                message="Server-script action inputs must be an object of at most twenty values.",
                details={"action_key": action_key},
            )
        inputs: list[AutomationScriptActionInput] = []
        for key, value in raw_inputs.items():
            if not isinstance(key, str) or not key.strip():
                raise AutomationScriptActionProtocolError(
                    code="automation.script_execution.action_contract_invalid",
                    message="Server-script input keys must be non-empty strings.",
                    details={"action_key": action_key},
                )
            inputs.append(AutomationScriptActionInput(key=key, value=value))
        requests.append(
            AutomationScriptActionRequest(
                action_key=action_key,
                inputs=tuple(inputs),
            )
        )
    return tuple(requests)


class AutomationScriptRunner:
    """Marshal one published server script through the hardened runner."""

    def __init__(self, *, transport: RunnerTransport | None = None) -> None:
        policy = current_policy()
        if runtime_state() is not AutomationScriptRuntimeState.ready or policy is None:
            raise AutomationScriptRuntimeUnavailable(
                code="automation.script_execution.runtime_unavailable",
                message="The isolated server-script runtime is not ready.",
                details={},
            )
        manifest = ConnectorManifest(
            key=_RUNTIME_KEY,
            name="Automation Center server-script runtime",
            version=_RUNTIME_VERSION,
            connector_type="automation_server_script",
            description="Executes one published JavaScript version in an isolated worker.",
            runtime=RuntimeManifest(
                type=ConnectorRuntimeType.external_oci,
                image=policy.image,
                digest=policy.digest,
            ),
            capabilities=(
                CapabilityManifest(
                    id=_CAPABILITY_ID,
                    modes=(CapabilityMode.event,),
                    description="Execute one tenant-scoped published server script.",
                ),
            ),
        )
        self._policy = policy
        self._runner = ExternalOciRunner(
            manifest,
            transport
            or PodmanTransport(
                egress=EgressPolicy(),
            ),
        )
        self._manifest = manifest

    def execute(
        self,
        db: Session,
        command: ExecuteAutomationServerScriptCommand,
    ) -> OperationResult:
        script, version = _published_version(db, command)
        installation_id = uuid5(NAMESPACE_URL, f"dotmac:automation-script:{script.id}")
        envelope = OperationEnvelope(
            operation_id=command.event_id,
            correlation_id=str(command.context.correlation_id),
            installation_id=installation_id,
            capability_binding_id=script.id,
            capability_id=_CAPABILITY_ID,
            connector_key=self._manifest.key,
            connector_version=self._manifest.version,
            manifest_digest=self._manifest.digest,
            config_revision_id=version.id,
            trigger=OperationTrigger.event,
            idempotency_key=(
                f"automation-script:{script.id}:{version.id}:{command.event_id}"
            ),
            deadline_at=datetime.now(UTC)
            + timedelta(seconds=self._policy.timeout_seconds),
            payload={
                "tenant_id": str(command.tenant_id),
                "script_id": str(script.id),
                "script_version_id": str(version.id),
                "target_type": script.target_type,
                "target_id": str(command.target_id),
                "event_name": command.event_name,
                "event_payload": dict(command.event_payload),
                "actor": command.context.actor,
                "source_code": version.source_code,
                "source_sha256": version.content_sha256,
                "language": script.language,
                "api_version": version.api_version,
            },
            actor=command.context.actor,
        )
        return self._runner.execute(
            envelope,
            config={
                "timeout_seconds": self._policy.timeout_seconds,
                "network": self._policy.network,
            },
            secret_material={},
        )


@dataclass(frozen=True, slots=True)
class ExecuteAutomationServerScriptCommand:
    """Typed input for a single published server-script invocation."""

    tenant_id: UUID
    script_id: UUID
    version_id: UUID
    event_id: UUID
    target_type: str
    target_id: UUID
    event_name: str
    event_payload: Mapping[str, Any]
    context: CommandContext


def _published_version(
    db: Session, command: ExecuteAutomationServerScriptCommand
) -> tuple[AutomationScript, AutomationScriptVersion]:
    row = db.execute(
        select(AutomationScript, AutomationScriptVersion)
        .join(
            AutomationScriptVersion,
            (AutomationScriptVersion.id == AutomationScript.active_version_id)
            & (AutomationScriptVersion.tenant_id == AutomationScript.tenant_id),
        )
        .where(
            AutomationScript.tenant_id == command.tenant_id,
            AutomationScript.id == command.script_id,
            AutomationScriptVersion.id == command.version_id,
            AutomationScript.kind == AutomationScriptKind.server.value,
            AutomationScript.status == AutomationScriptStatus.published.value,
            AutomationScript.target_type == command.target_type,
            AutomationScript.event_name == command.event_name,
        )
    ).one_or_none()
    if row is None:
        raise AutomationScriptExecutionError(
            code="automation.script_execution.not_published",
            message="The requested server script is not published for this event.",
            details={"script_id": str(command.script_id)},
        )
    return row


__all__ = [
    "AutomationScriptActionInput",
    "AutomationScriptActionProtocolError",
    "AutomationScriptActionRequest",
    "AutomationScriptExecutionError",
    "AutomationScriptRunner",
    "AutomationScriptRuntimeUnavailable",
    "ExecuteAutomationServerScriptCommand",
    "parse_script_action_requests",
]
