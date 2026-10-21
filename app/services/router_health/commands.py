"""Owner commands for the router health reconciler (``network.router_health``).

Network I/O never happens here. Adapters (tasks) collect first on a session that
has done no writes, then call exactly one command on a transaction-free session.
Every command is flush-only inside ``execute_owner_command``, which commits once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.event_store import EventStatus
from app.models.router_health import RouterHealthSignal
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.event_store import create_event_record
from app.services.events.types import Event, EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.router_health import findings as findings_service
from app.services.router_health.contracts import (
    CanaryDisposition,
    ConfigExportOutcome,
    HealthFinding,
    HealthLayer,
    SignalState,
)

OWNER = "network.router_health"
CONCERN_EXPORT_EVIDENCE = "router config export evidence"
CONCERN_PROJECTION = "router health findings projection"
CONCERN_CANARY = "CoA canary operation"
CONCERN_RUNTIME = "router runtime observations"
ACTOR = "system:router_health"
AUDIT_ACTOR = AuditActor(actor_type=AuditActorType.system, actor_id=ACTOR)
SIGNAL_CONFIG_EXPORT = "config_export"
SCOPE_ROUTER = "router"
SAMPLE_LIMIT = 200

AUDIT_CANARY_STARTED = "router_health.coa_canary.started"
AUDIT_CANARY_COMPLETED = "router_health.coa_canary.completed"
AUDIT_ENTITY_CANARY = "router_health_coa_canary"
AUDIT_RETIRED = "router_health.findings.retired"


def system_context(reason: str) -> CommandContext:
    return CommandContext.system(actor=ACTOR, scope="network", reason=reason)


def _definition(concern: str, name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(owner=OWNER, concern=concern, name=name)


def _stage_event(
    db: Session, context: CommandContext, payload: dict[str, object]
) -> None:
    create_event_record(
        db,
        event=Event(
            event_type=EventType.router_health_reconciled,
            event_id=context.command_id,
            actor=context.actor,
            payload={
                "schema_version": 1,
                "correlation_id": str(context.correlation_id),
                **payload,
            },
        ),
        status=EventStatus.pending,
    )


# --------------------------------------------------------------- export evidence


@dataclass(frozen=True)
class RecordConfigExportOutcomesCommand:
    context: CommandContext
    outcomes: tuple[ConfigExportOutcome, ...]


def _upsert_export_signal(db: Session, outcome: ConfigExportOutcome) -> None:
    row = db.scalar(
        select(RouterHealthSignal).where(
            RouterHealthSignal.router_id == outcome.router_id,
            RouterHealthSignal.signal == SIGNAL_CONFIG_EXPORT,
            RouterHealthSignal.scope == SCOPE_ROUTER,
        )
    )
    value = 1 if outcome.ok else 0
    sample = [int(outcome.at.timestamp()), value]
    if row is None:
        row = RouterHealthSignal(
            router_id=outcome.router_id,
            signal=SIGNAL_CONFIG_EXPORT,
            scope=SCOPE_ROUTER,
            status="ok",
            samples=[],
            consecutive_abnormal=0,
            last_observed_at=outcome.at,
        )
        db.add(row)
    samples = list(row.samples or [])
    samples.append(sample)
    row.samples = samples[-SAMPLE_LIMIT:]
    row.last_observed_at = outcome.at
    row.last_value = Decimal(value)
    if outcome.ok:
        row.status = "ok"
        row.last_text = None
        row.consecutive_abnormal = 0
        row.first_abnormal_at = None
        row.last_normal_at = outcome.at
    else:
        row.status = "abnormal"
        # Only the failure class is stored, never exception text.
        row.last_text = (
            outcome.failure_class.value if outcome.failure_class is not None else None
        )
        row.consecutive_abnormal = int(row.consecutive_abnormal or 0) + 1
        if row.first_abnormal_at is None:
            row.first_abnormal_at = outcome.at
    db.flush()


def record_config_export_outcomes(
    db: Session, *, command: RecordConfigExportOutcomesCommand
) -> int:
    """Upsert one ``config_export`` signal per router for a snapshot run."""

    def operation() -> int:
        for outcome in command.outcomes:
            _upsert_export_signal(db, outcome)
        failed = sum(1 for outcome in command.outcomes if not outcome.ok)
        _stage_event(
            db,
            command.context,
            {
                "kind": "config_export_outcomes",
                "routers": len(command.outcomes),
                "failed": failed,
            },
        )
        return len(command.outcomes)

    return execute_owner_command(
        db,
        definition=_definition(
            CONCERN_EXPORT_EVIDENCE, "record_config_export_outcomes"
        ),
        context=command.context,
        operation=operation,
    )


# ------------------------------------------------------------------ projections


@dataclass(frozen=True)
class ProjectFindingsCommand:
    """Findings judged this run plus the scopes they fully replace."""

    context: CommandContext
    findings: tuple[HealthFinding, ...]
    scopes: tuple[findings_service.Scope, ...]
    mode: str
    retire_subjects: tuple[tuple[HealthLayer, UUID], ...] = ()
    capped: int = 0


def _project(
    db: Session, command: ProjectFindingsCommand, kind: str
) -> findings_service.SyncOutcome:
    outcome = findings_service.sync_findings(
        db, findings=command.findings, scopes=command.scopes, mode=command.mode
    )
    retired = 0
    for layer, subject_id in command.retire_subjects:
        retired += findings_service.resolve_subjects(
            db, layer=layer, subject_ids=[subject_id]
        )
    _stage_event(
        db,
        command.context,
        {
            "kind": kind,
            "mode": command.mode,
            "findings": len(command.findings),
            "opened": outcome.opened,
            "resolved": outcome.resolved + retired,
            "capped": command.capped,
        },
    )
    return findings_service.SyncOutcome(
        opened=outcome.opened,
        updated=outcome.updated,
        escalated=outcome.escalated,
        resolved=outcome.resolved + retired,
        capped=command.capped,
    )


def reconcile_router_posture(
    db: Session, *, command: ProjectFindingsCommand
) -> findings_service.SyncOutcome:
    return execute_owner_command(
        db,
        definition=_definition(CONCERN_PROJECTION, "reconcile_router_posture"),
        context=command.context,
        operation=lambda: _project(db, command, "posture"),
    )


def reconcile_service_path(
    db: Session, *, command: ProjectFindingsCommand
) -> findings_service.SyncOutcome:
    return execute_owner_command(
        db,
        definition=_definition(CONCERN_PROJECTION, "reconcile_service_path"),
        context=command.context,
        operation=lambda: _project(db, command, "service"),
    )


@dataclass(frozen=True)
class RetireFindingsCommand:
    context: CommandContext


def retire_router_health_findings(
    db: Session, *, command: RetireFindingsCommand
) -> int:
    """Rollback path: resolve every ``network:router-health:`` alert (audited)."""

    def operation() -> int:
        resolved = findings_service.resolve_all(db)
        stage_audit_event(
            db,
            action=AUDIT_RETIRED,
            entity_type="router_health",
            entity_id=str(command.context.command_id),
            actor=AUDIT_ACTOR,
            metadata={"resolved": resolved, "reason": command.context.reason},
        )
        _stage_event(db, command.context, {"kind": "retired", "resolved": resolved})
        return resolved

    return execute_owner_command(
        db,
        definition=_definition(CONCERN_PROJECTION, "retire_router_health_findings"),
        context=command.context,
        operation=operation,
    )


# --------------------------------------------------------------- runtime (PR2)


@dataclass(frozen=True)
class RouterRuntimeResult:
    """One router's next signal states and the signals to forget."""

    router_id: UUID
    signals: tuple[SignalState, ...]
    retire: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RecordRuntimePollCommand:
    context: CommandContext
    results: tuple[RouterRuntimeResult, ...]
    findings: tuple[HealthFinding, ...]
    scopes: tuple[findings_service.Scope, ...]
    mode: str
    retire_subjects: tuple[tuple[HealthLayer, UUID], ...] = ()
    capped: int = 0


def _write_signal(
    db: Session,
    router_id: UUID,
    state: SignalState,
    existing: RouterHealthSignal | None,
) -> None:
    row = existing
    if row is None:
        row = RouterHealthSignal(
            router_id=router_id,
            signal=state.signal,
            scope=state.scope,
            samples=[],
            consecutive_abnormal=0,
        )
        db.add(row)
    row.status = state.status
    row.last_value = (
        None if state.last_value is None else Decimal(str(round(state.last_value, 4)))
    )
    row.last_text = state.last_text
    row.samples = [[ts, value] for ts, value in state.samples[-SAMPLE_LIMIT:]]
    row.first_abnormal_at = state.first_abnormal_at
    row.last_normal_at = state.last_normal_at
    row.last_observed_at = state.last_observed_at
    row.consecutive_abnormal = state.consecutive_abnormal
    row.boot_marker = state.boot_marker


def _apply_runtime_results(db: Session, results: Sequence[RouterRuntimeResult]) -> int:
    written = 0
    for result in results:
        rows = {
            (row.signal, row.scope): row
            for row in db.execute(
                select(RouterHealthSignal).where(
                    RouterHealthSignal.router_id == result.router_id
                )
            ).scalars()
        }
        for state in result.signals:
            _write_signal(
                db, result.router_id, state, rows.get((state.signal, state.scope))
            )
            written += 1
        for signal, scope in result.retire:
            db.execute(
                delete(RouterHealthSignal).where(
                    RouterHealthSignal.router_id == result.router_id,
                    RouterHealthSignal.signal == signal,
                    RouterHealthSignal.scope == scope,
                )
            )
    db.flush()
    return written


def record_runtime_poll(
    db: Session, *, command: RecordRuntimePollCommand
) -> findings_service.SyncOutcome:
    """Persist runtime signals and project runtime findings (one commit)."""

    def operation() -> findings_service.SyncOutcome:
        written = _apply_runtime_results(db, command.results)
        outcome = findings_service.sync_findings(
            db, findings=command.findings, scopes=command.scopes, mode=command.mode
        )
        retired = 0
        for layer, subject_id in command.retire_subjects:
            retired += findings_service.resolve_subjects(
                db, layer=layer, subject_ids=[subject_id]
            )
        _stage_event(
            db,
            command.context,
            {
                "kind": "runtime_poll",
                "mode": command.mode,
                "routers": len(command.results),
                "signals": written,
                "findings": len(command.findings),
                "opened": outcome.opened,
                "resolved": outcome.resolved + retired,
                "capped": command.capped,
            },
        )
        return findings_service.SyncOutcome(
            opened=outcome.opened,
            updated=outcome.updated,
            escalated=outcome.escalated,
            resolved=outcome.resolved + retired,
            capped=command.capped,
        )

    return execute_owner_command(
        db,
        definition=_definition(CONCERN_RUNTIME, "record_runtime_poll"),
        context=command.context,
        operation=operation,
    )


# ----------------------------------------------------------------------- canary


@dataclass(frozen=True)
class CanaryTargetSummary:
    nas_device_ids: tuple[UUID, ...]
    host: str


@dataclass(frozen=True)
class BeginCanaryRunCommand:
    context: CommandContext
    run_id: UUID
    targets: tuple[CanaryTargetSummary, ...]
    username_prefix: str


def begin_coa_canary_run(db: Session, *, command: BeginCanaryRunCommand) -> UUID:
    """Commit the audit ``started`` row BEFORE any packet is sent."""

    def operation() -> UUID:
        stage_audit_event(
            db,
            action=AUDIT_CANARY_STARTED,
            entity_type=AUDIT_ENTITY_CANARY,
            entity_id=str(command.run_id),
            actor=AUDIT_ACTOR,
            metadata={
                "target_count": len(command.targets),
                "nas_device_ids": [
                    str(nas_id) for t in command.targets for nas_id in t.nas_device_ids
                ],
                "hosts": [t.host for t in command.targets],
                "username_prefix": command.username_prefix,
            },
        )
        return command.run_id

    return execute_owner_command(
        db,
        definition=_definition(CONCERN_CANARY, "begin_coa_canary_run"),
        context=command.context,
        operation=operation,
    )


@dataclass(frozen=True)
class CompleteCanaryRunCommand:
    context: CommandContext
    run_id: UUID
    dispositions: tuple[tuple[UUID, CanaryDisposition], ...]
    findings: tuple[HealthFinding, ...]
    scopes: tuple[findings_service.Scope, ...]
    mode: str
    reprobe: bool = False


def complete_coa_canary_run(
    db: Session, *, command: CompleteCanaryRunCommand
) -> findings_service.SyncOutcome:
    def operation() -> findings_service.SyncOutcome:
        outcome = findings_service.sync_findings(
            db, findings=command.findings, scopes=command.scopes, mode=command.mode
        )
        stage_audit_event(
            db,
            action=AUDIT_CANARY_COMPLETED,
            entity_type=AUDIT_ENTITY_CANARY,
            entity_id=str(command.run_id),
            actor=AUDIT_ACTOR,
            metadata={
                "reprobe": command.reprobe,
                "results": {
                    str(nas_id): disposition.value
                    for nas_id, disposition in command.dispositions
                },
            },
        )
        _stage_event(
            db,
            command.context,
            {
                "kind": "canary_completed",
                "mode": command.mode,
                "results": len(command.dispositions),
                "findings": len(command.findings),
            },
        )
        return outcome

    return execute_owner_command(
        db,
        definition=_definition(CONCERN_CANARY, "complete_coa_canary_run"),
        context=command.context,
        operation=operation,
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__: Sequence[str] = [
    "BeginCanaryRunCommand",
    "CanaryTargetSummary",
    "CompleteCanaryRunCommand",
    "ProjectFindingsCommand",
    "RecordConfigExportOutcomesCommand",
    "RecordRuntimePollCommand",
    "RouterRuntimeResult",
    "RetireFindingsCommand",
    "begin_coa_canary_run",
    "complete_coa_canary_run",
    "reconcile_router_posture",
    "reconcile_service_path",
    "record_config_export_outcomes",
    "record_runtime_poll",
    "retire_router_health_findings",
    "system_context",
]
