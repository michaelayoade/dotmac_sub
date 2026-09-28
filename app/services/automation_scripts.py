"""Typed lifecycle for native Automation Center client and server scripts.

Scripts are control-plane records. Their source is immutable after a version is
created; execution is a separate adapter and is deliberately not implemented by
evaluating source code in the web or worker process.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.automation_scripts import (
    AutomationScript,
    AutomationScriptKind,
    AutomationScriptLanguage,
    AutomationScriptRun,
    AutomationScriptRunStatus,
    AutomationScriptStatus,
    AutomationScriptVersion,
)
from app.services import automation_capabilities
from app.services.automation_script_runtime import (
    AutomationScriptRuntimeState,
    runtime_state,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "automation.script_definitions"
SCRIPT_READ_PERMISSION = "automation:script:read"
SCRIPT_CREATE_PERMISSION = "automation:script:create"
SCRIPT_UPDATE_PERMISSION = "automation:script:update"
SCRIPT_PUBLISH_PERMISSION = "automation:script:publish"

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_MAX_SOURCE_LENGTH = 64 * 1024
_FORBIDDEN_SOURCE_PATTERNS = (
    re.compile(r"\b(?:require|import|export)\b"),
    re.compile(
        r"\b(?:process|globalThis|__dirname|__filename|window|document|fetch|"
        r"XMLHttpRequest|WebSocket|Worker|importScripts|navigator|location|"
        r"localStorage|sessionStorage|history|postMessage)\b"
    ),
    re.compile(r"\b(?:eval|Function|WebAssembly)\s*\("),
)

_CREATE = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation script definitions and immutable versions",
    name="create_automation_script",
)
_PUBLISH = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation script definitions and immutable versions",
    name="publish_automation_script",
)
_SCRIPT_RUN = OwnerCommandDefinition(
    owner=OWNER,
    concern="automation script execution evidence",
    name="record_automation_script_run",
)


class AutomationScriptError(DomainError):
    """Fail-closed script authoring error."""


@dataclass(frozen=True, slots=True)
class CreateAutomationScriptCommand:
    tenant_id: UUID
    key: str
    name: str
    description: str | None
    kind: AutomationScriptKind
    language: AutomationScriptLanguage
    target_type: str
    event_name: str
    source_code: str
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PublishAutomationScriptCommand:
    tenant_id: UUID
    script_id: UUID
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationScriptOutcome:
    script_id: UUID
    version_id: UUID
    version: int
    status: AutomationScriptStatus


@dataclass(frozen=True, slots=True)
class AutomationScriptSummary:
    script_id: UUID
    key: str
    name: str
    kind: AutomationScriptKind
    target_type: str
    event_name: str
    status: AutomationScriptStatus
    version: int


@dataclass(frozen=True, slots=True)
class PublishedAutomationServerScript:
    script_id: UUID
    version_id: UUID
    target_type: str
    event_name: str


@dataclass(frozen=True, slots=True)
class PublishedAutomationClientScript:
    script_id: UUID
    version_id: UUID
    key: str
    target_type: str
    event_name: str
    source_code: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class StartAutomationScriptRunCommand:
    tenant_id: UUID
    script_id: UUID
    version_id: UUID
    event_id: UUID
    target_type: str
    target_id: UUID
    context: CommandContext


@dataclass(frozen=True, slots=True)
class AutomationScriptRunAdmission:
    run_id: UUID
    should_execute: bool


@dataclass(frozen=True, slots=True)
class FinishAutomationScriptRunCommand:
    tenant_id: UUID
    run_id: UUID
    status: AutomationScriptRunStatus
    result_code: str | None
    error_code: str | None
    context: CommandContext


def _error(code: str, message: str, **details: object) -> AutomationScriptError:
    return AutomationScriptError(
        code=f"{OWNER}.{code}", message=message, details=details
    )


def _require_permission(permission_keys: frozenset[str], permission: str) -> None:
    if "*" not in permission_keys and permission not in permission_keys:
        raise _error(
            "permission_denied",
            "The script command is not authorized.",
            required_permission=permission,
        )


def _target(kind: AutomationScriptKind, target_type: str, event_name: str):
    matches = [
        item
        for manifest in automation_capabilities.registered_module_manifests()
        for item in manifest.script_targets
        if item.entity_type == target_type
    ]
    if len(matches) != 1:
        raise _error(
            "target_undeclared",
            "The selected script target is not registered.",
            target_type=target_type,
        )
    events = (
        matches[0].client_events
        if kind is AutomationScriptKind.client
        else matches[0].server_events
    )
    if event_name not in events:
        raise _error(
            "event_undeclared",
            "The selected script event is not registered for this target.",
            target_type=target_type,
            event_name=event_name,
        )
    return matches[0]


def _validate_source(source_code: str) -> str:
    source = source_code.strip()
    if not source:
        raise _error("source_empty", "Script source is required.")
    if len(source) > _MAX_SOURCE_LENGTH:
        raise _error("source_too_large", "Script source exceeds the 64 KiB limit.")
    for pattern in _FORBIDDEN_SOURCE_PATTERNS:
        if pattern.search(source):
            raise _error(
                "source_uses_forbidden_api",
                "The script uses an API unavailable in the isolated runtime.",
            )
    return source


def _emit_change(
    db: Session,
    *,
    script: AutomationScript,
    version: AutomationScriptVersion,
    change: str,
) -> None:
    emit_event(
        db,
        EventType.automation_script_changed,
        {
            "tenant_id": str(script.tenant_id),
            "script_id": str(script.id),
            "version_id": str(version.id),
            "version": version.version,
            "target_type": script.target_type,
            "event_name": script.event_name,
            "kind": script.kind,
            "change": change,
            "status": script.status,
        },
        actor=OWNER,
    )


def create_script(
    db: Session, command: CreateAutomationScriptCommand
) -> AutomationScriptOutcome:
    """Create a draft and immutable version through the script owner boundary."""

    def operation() -> AutomationScriptOutcome:
        _require_permission(command.permission_keys, SCRIPT_CREATE_PERMISSION)
        if command.language is not AutomationScriptLanguage.javascript:
            raise _error(
                "language_unsupported",
                "Only JavaScript is admitted by the script control plane.",
            )
        key = command.key.strip()
        name = command.name.strip()
        if not _KEY_PATTERN.fullmatch(key) or not name:
            raise _error("identity_invalid", "Script key or name is invalid.")
        target = _target(
            command.kind, command.target_type.strip(), command.event_name.strip()
        )
        source = _validate_source(command.source_code)
        existing = db.scalar(
            select(AutomationScript)
            .where(
                AutomationScript.tenant_id == command.tenant_id,
                AutomationScript.key == key,
            )
            .with_for_update()
        )
        if existing is not None:
            raise _error("key_conflict", "A script with this key already exists.")
        script = AutomationScript(
            tenant_id=command.tenant_id,
            key=key,
            name=name,
            description=(command.description or "").strip() or None,
            kind=command.kind.value,
            language=command.language.value,
            target_type=target.entity_type,
            event_name=command.event_name.strip(),
            status=AutomationScriptStatus.draft.value,
            created_by=command.context.actor,
        )
        db.add(script)
        db.flush()
        version = AutomationScriptVersion(
            tenant_id=command.tenant_id,
            script_id=script.id,
            version=1,
            source_code=source,
            content_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            api_version=1,
            limits={"max_source_bytes": _MAX_SOURCE_LENGTH, "network": "none"},
            created_by=command.context.actor,
        )
        db.add(version)
        db.flush()
        _emit_change(db, script=script, version=version, change="created")
        return AutomationScriptOutcome(
            script.id, version.id, version.version, AutomationScriptStatus.draft
        )

    return execute_owner_command(
        db, definition=_CREATE, context=command.context, operation=operation
    )


def publish_script(
    db: Session, command: PublishAutomationScriptCommand
) -> AutomationScriptOutcome:
    """Publish one immutable version after target and runtime admission checks."""

    def operation() -> AutomationScriptOutcome:
        _require_permission(command.permission_keys, SCRIPT_PUBLISH_PERMISSION)
        script = db.scalar(
            select(AutomationScript)
            .where(
                AutomationScript.tenant_id == command.tenant_id,
                AutomationScript.id == command.script_id,
            )
            .with_for_update()
        )
        if script is None:
            raise _error("not_found", "The script was not found.")
        if script.status == AutomationScriptStatus.retired.value:
            raise _error("retired", "A retired script cannot be published.")
        kind = AutomationScriptKind(script.kind)
        target = _target(kind, script.target_type, script.event_name)
        _require_permission(command.permission_keys, target.read_permission)
        _require_permission(command.permission_keys, target.write_permission)
        version = db.scalar(
            select(AutomationScriptVersion)
            .where(
                AutomationScriptVersion.tenant_id == command.tenant_id,
                AutomationScriptVersion.script_id == script.id,
                AutomationScriptVersion.version == 1,
            )
            .with_for_update()
        )
        if version is None:
            raise _error("version_missing", "The script has no publishable version.")
        if (
            kind is AutomationScriptKind.server
            and runtime_state() is not AutomationScriptRuntimeState.ready
        ):
            raise _error(
                "runtime_unavailable",
                "The isolated server-script runtime is not ready.",
            )
        script.active_version_id = version.id
        script.status = AutomationScriptStatus.published.value
        version.published_by = command.context.actor
        version.published_at = datetime.now(UTC)
        db.flush()
        _emit_change(db, script=script, version=version, change="published")
        return AutomationScriptOutcome(
            script.id,
            version.id,
            version.version,
            AutomationScriptStatus.published,
        )

    return execute_owner_command(
        db, definition=_PUBLISH, context=command.context, operation=operation
    )


def start_script_run(
    db: Session, command: StartAutomationScriptRunCommand
) -> AutomationScriptRunAdmission:
    """Create one redacted running receipt, refusing duplicate event replay."""

    def operation() -> AutomationScriptRunAdmission:
        existing = db.scalar(
            select(AutomationScriptRun)
            .where(
                AutomationScriptRun.tenant_id == command.tenant_id,
                AutomationScriptRun.script_version_id == command.version_id,
                AutomationScriptRun.event_id == command.event_id,
            )
            .with_for_update()
        )
        if existing is not None:
            return AutomationScriptRunAdmission(existing.id, False)
        run = AutomationScriptRun(
            tenant_id=command.tenant_id,
            script_id=command.script_id,
            script_version_id=command.version_id,
            event_id=command.event_id,
            target_type=command.target_type,
            target_id=command.target_id,
            status=AutomationScriptRunStatus.running.value,
            started_at=datetime.now(UTC),
        )
        db.add(run)
        db.flush()
        return AutomationScriptRunAdmission(run.id, True)

    return execute_owner_command(
        db, definition=_SCRIPT_RUN, context=command.context, operation=operation
    )


def finish_script_run(db: Session, command: FinishAutomationScriptRunCommand) -> None:
    """Store only typed status/error evidence; source and payload never persist."""

    def operation() -> None:
        run = db.scalar(
            select(AutomationScriptRun)
            .where(
                AutomationScriptRun.tenant_id == command.tenant_id,
                AutomationScriptRun.id == command.run_id,
            )
            .with_for_update()
        )
        if run is None:
            raise _error("run_not_found", "The script run was not found.")
        if run.completed_at is not None:
            return
        run.status = command.status.value
        run.result_code = command.result_code
        run.error_code = command.error_code
        run.completed_at = datetime.now(UTC)
        db.flush()

    execute_owner_command(
        db, definition=_SCRIPT_RUN, context=command.context, operation=operation
    )


def list_scripts(
    db: Session, *, tenant_id: UUID
) -> tuple[AutomationScriptSummary, ...]:
    rows = db.execute(
        select(AutomationScript, AutomationScriptVersion)
        .join(
            AutomationScriptVersion,
            (AutomationScriptVersion.script_id == AutomationScript.id)
            & (AutomationScriptVersion.tenant_id == AutomationScript.tenant_id),
        )
        .where(
            AutomationScript.tenant_id == tenant_id,
            AutomationScriptVersion.tenant_id == tenant_id,
        )
        .order_by(AutomationScript.updated_at.desc(), AutomationScript.key.asc())
    ).all()
    return tuple(
        AutomationScriptSummary(
            script_id=script.id,
            key=script.key,
            name=script.name,
            kind=AutomationScriptKind(script.kind),
            target_type=script.target_type,
            event_name=script.event_name,
            status=AutomationScriptStatus(script.status),
            version=version.version,
        )
        for script, version in rows
    )


def published_server_scripts(
    db: Session,
    *,
    tenant_id: UUID,
    target_type: str,
    event_name: str,
) -> tuple[PublishedAutomationServerScript, ...]:
    """Return the immutable server-script projections matching one event."""

    rows = db.execute(
        select(AutomationScript, AutomationScriptVersion)
        .join(
            AutomationScriptVersion,
            (AutomationScriptVersion.id == AutomationScript.active_version_id)
            & (AutomationScriptVersion.tenant_id == AutomationScript.tenant_id),
        )
        .where(
            AutomationScript.tenant_id == tenant_id,
            AutomationScript.kind == AutomationScriptKind.server.value,
            AutomationScript.status == AutomationScriptStatus.published.value,
            AutomationScript.target_type == target_type,
            AutomationScript.event_name == event_name,
        )
        .order_by(AutomationScript.key.asc())
    ).all()
    return tuple(
        PublishedAutomationServerScript(
            script_id=script.id,
            version_id=version.id,
            target_type=script.target_type,
            event_name=script.event_name,
        )
        for script, version in rows
    )


def published_client_scripts(
    db: Session,
    *,
    tenant_id: UUID,
    target_type: str,
    event_name: str,
) -> tuple[PublishedAutomationClientScript, ...]:
    """Return executable client-script versions for one declared form event."""

    rows = db.execute(
        select(AutomationScript, AutomationScriptVersion)
        .join(
            AutomationScriptVersion,
            (AutomationScriptVersion.id == AutomationScript.active_version_id)
            & (AutomationScriptVersion.tenant_id == AutomationScript.tenant_id),
        )
        .where(
            AutomationScript.tenant_id == tenant_id,
            AutomationScript.kind == AutomationScriptKind.client.value,
            AutomationScript.status == AutomationScriptStatus.published.value,
            AutomationScript.target_type == target_type,
            AutomationScript.event_name == event_name,
        )
        .order_by(AutomationScript.key.asc())
    ).all()
    return tuple(
        PublishedAutomationClientScript(
            script_id=script.id,
            version_id=version.id,
            key=script.key,
            target_type=script.target_type,
            event_name=script.event_name,
            source_code=version.source_code,
            content_sha256=version.content_sha256,
        )
        for script, version in rows
    )


__all__ = [
    "AutomationScriptError",
    "AutomationScriptOutcome",
    "AutomationScriptRunAdmission",
    "AutomationScriptSummary",
    "CreateAutomationScriptCommand",
    "FinishAutomationScriptRunCommand",
    "PublishedAutomationServerScript",
    "PublishedAutomationClientScript",
    "PublishAutomationScriptCommand",
    "SCRIPT_CREATE_PERMISSION",
    "SCRIPT_PUBLISH_PERMISSION",
    "SCRIPT_READ_PERMISSION",
    "SCRIPT_UPDATE_PERMISSION",
    "create_script",
    "finish_script_run",
    "list_scripts",
    "published_server_scripts",
    "published_client_scripts",
    "publish_script",
    "StartAutomationScriptRunCommand",
    "start_script_run",
]
