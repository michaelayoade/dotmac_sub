"""Persistent library commands for AI Intake canary scenarios."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_intake import (
    AiIntakeCanaryRun,
    AiIntakeCanaryScenario,
    AiIntakeCanaryScenarioRevision,
    AiIntakeCanarySuite,
    AiIntakeCanarySuiteScenario,
    AiIntakePolicyVersion,
)
from app.services.ai_intake_canary_runner import (
    CanaryEngineMode,
    CanaryPolicySelection,
    CanaryRunResult,
    CanaryScenarioDefinition,
    run_canary_scenario,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "ai.intake_canaries"
_SCENARIO_LIBRARY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="AI intake canary scenario library lifecycle",
    name="save_ai_intake_canary_scenario_revision",
)
_RUN_EVIDENCE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="AI intake canary run evidence",
    name="record_ai_intake_canary_run",
)
_SUITE_LIBRARY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="AI intake canary scenario library lifecycle",
    name="save_ai_intake_canary_suite",
)


@dataclass(frozen=True, slots=True)
class SaveCanaryScenarioCommand:
    context: CommandContext
    definition: CanaryScenarioDefinition
    actor_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SaveCanaryScenarioOutcome:
    scenario_id: UUID
    scenario_key: str
    revision_id: UUID
    revision_number: int
    definition_sha256: str


@dataclass(frozen=True, slots=True)
class RunCanaryScenarioCommand:
    context: CommandContext
    scenario_id: UUID
    policy_version_id: UUID | None = None
    policy_selection: CanaryPolicySelection | None = None
    suite_id: UUID | None = None
    actor_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RunCanaryScenarioOutcome:
    run_id: UUID
    scenario_id: UUID
    scenario_revision_id: UUID
    passed: bool
    result: CanaryRunResult


@dataclass(frozen=True, slots=True)
class CanarySuiteRow:
    suite_id: UUID
    suite_key: str
    name: str
    description: str | None
    enabled: bool
    required_for_activation: bool
    scenario_count: int


@dataclass(frozen=True, slots=True)
class SaveCanarySuiteCommand:
    context: CommandContext
    suite_key: str
    name: str
    description: str | None = None
    enabled: bool = True
    required_for_activation: bool = False
    scenario_ids: tuple[UUID, ...] = ()
    actor_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SaveCanarySuiteOutcome:
    suite_id: UUID
    suite_key: str
    scenario_count: int


@dataclass(frozen=True, slots=True)
class RunCanarySuiteCommand:
    context: CommandContext
    suite_id: UUID
    policy_version_id: UUID | None = None
    policy_selection: CanaryPolicySelection | None = None
    actor_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RunCanarySuiteOutcome:
    suite_id: UUID
    run_ids: tuple[UUID, ...]
    passed: bool
    scenario_results: tuple[RunCanaryScenarioOutcome, ...]


def save_scenario_revision(
    db: Session, command: SaveCanaryScenarioCommand
) -> SaveCanaryScenarioOutcome:
    def operation() -> SaveCanaryScenarioOutcome:
        return _save_scenario_revision_locked(db, command)

    return execute_owner_command(
        db,
        definition=_SCENARIO_LIBRARY_COMMAND,
        context=command.context,
        operation=operation,
    )


def run_persisted_scenario(
    db: Session, command: RunCanaryScenarioCommand
) -> RunCanaryScenarioOutcome:
    def operation() -> RunCanaryScenarioOutcome:
        return _run_persisted_scenario_locked(db, command)

    return execute_owner_command(
        db,
        definition=_RUN_EVIDENCE_COMMAND,
        context=command.context,
        operation=operation,
    )


def save_suite(db: Session, command: SaveCanarySuiteCommand) -> SaveCanarySuiteOutcome:
    def operation() -> SaveCanarySuiteOutcome:
        return _save_suite_locked(db, command)

    return execute_owner_command(
        db,
        definition=_SUITE_LIBRARY_COMMAND,
        context=command.context,
        operation=operation,
    )


def run_suite(db: Session, command: RunCanarySuiteCommand) -> RunCanarySuiteOutcome:
    def operation() -> RunCanarySuiteOutcome:
        return _run_suite_locked(db, command)

    return execute_owner_command(
        db,
        definition=_RUN_EVIDENCE_COMMAND,
        context=command.context,
        operation=operation,
    )


def list_suites(db: Session) -> tuple[CanarySuiteRow, ...]:
    suites = (
        db.query(AiIntakeCanarySuite).order_by(AiIntakeCanarySuite.name.asc()).all()
    )
    counts = {
        suite_id: count
        for suite_id, count in (
            db.query(
                AiIntakeCanarySuiteScenario.suite_id,
                func.count(AiIntakeCanarySuiteScenario.id),
            )
            .group_by(AiIntakeCanarySuiteScenario.suite_id)
            .all()
        )
    }
    return tuple(
        CanarySuiteRow(
            suite_id=suite.id,
            suite_key=suite.suite_key,
            name=suite.name,
            description=suite.description,
            enabled=suite.enabled,
            required_for_activation=suite.required_for_activation,
            scenario_count=int(counts.get(suite.id, 0)),
        )
        for suite in suites
    )


def scenario_id_for_key(db: Session, scenario_key: str) -> UUID | None:
    scenario = (
        db.query(AiIntakeCanaryScenario.id)
        .filter(AiIntakeCanaryScenario.scenario_key == scenario_key)
        .one_or_none()
    )
    return scenario[0] if scenario is not None else None


def scenario_ids_for_keys(
    db: Session, scenario_keys: tuple[str, ...]
) -> tuple[UUID, ...]:
    resolved: list[UUID] = []
    for value in scenario_keys:
        try:
            resolved.append(UUID(value))
            continue
        except ValueError:
            pass
        scenario_id = scenario_id_for_key(db, value)
        if scenario_id is not None:
            resolved.append(scenario_id)
    return tuple(resolved)


def scenario_ids_for_run_scope(db: Session, *, required_only: bool) -> tuple[UUID, ...]:
    query = db.query(AiIntakeCanaryScenario.id).filter(
        AiIntakeCanaryScenario.enabled.is_(True)
    )
    if required_only:
        query = query.filter(AiIntakeCanaryScenario.required_for_activation.is_(True))
    rows = query.order_by(
        AiIntakeCanaryScenario.priority.asc(),
        AiIntakeCanaryScenario.name.asc(),
    ).all()
    return tuple(row[0] for row in rows)


def _save_scenario_revision_locked(
    db: Session, command: SaveCanaryScenarioCommand
) -> SaveCanaryScenarioOutcome:
    definition = command.definition
    scenario = (
        db.query(AiIntakeCanaryScenario)
        .filter(AiIntakeCanaryScenario.scenario_key == definition.scenario_id)
        .with_for_update()
        .one_or_none()
    )
    if scenario is None:
        scenario = AiIntakeCanaryScenario(
            scenario_key=definition.scenario_id,
            name=definition.name,
            description=definition.description,
            enabled=definition.enabled,
            required_for_activation=definition.required_for_activation,
            priority=definition.priority,
            tags=list(definition.tags),
            created_by_person_id=command.actor_person_id,
            updated_by_person_id=command.actor_person_id,
        )
        db.add(scenario)
        db.flush()
        revision_number = 1
    else:
        latest = (
            db.query(AiIntakeCanaryScenarioRevision)
            .filter(AiIntakeCanaryScenarioRevision.scenario_id == scenario.id)
            .order_by(AiIntakeCanaryScenarioRevision.revision_number.desc())
            .first()
        )
        revision_number = 1 if latest is None else latest.revision_number + 1
        scenario.name = definition.name
        scenario.description = definition.description
        scenario.enabled = definition.enabled
        scenario.required_for_activation = definition.required_for_activation
        scenario.priority = definition.priority
        scenario.tags = list(definition.tags)
        scenario.updated_by_person_id = command.actor_person_id

    revision_definition = definition.model_copy(update={"revision": revision_number})
    payload = revision_definition.model_dump(mode="json")
    digest = _payload_sha256(payload)
    revision = AiIntakeCanaryScenarioRevision(
        scenario_id=scenario.id,
        revision_number=revision_number,
        definition=payload,
        definition_sha256=digest,
        created_by_person_id=command.actor_person_id,
    )
    db.add(revision)
    db.flush()
    scenario.current_revision_id = revision.id
    return SaveCanaryScenarioOutcome(
        scenario_id=scenario.id,
        scenario_key=scenario.scenario_key,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        definition_sha256=digest,
    )


def _run_persisted_scenario_locked(
    db: Session, command: RunCanaryScenarioCommand
) -> RunCanaryScenarioOutcome:
    scenario = (
        db.query(AiIntakeCanaryScenario)
        .filter(AiIntakeCanaryScenario.id == command.scenario_id)
        .with_for_update()
        .one_or_none()
    )
    if scenario is None or scenario.current_revision_id is None:
        raise ValueError("AI intake canary scenario was not found")
    revision = db.get(AiIntakeCanaryScenarioRevision, scenario.current_revision_id)
    if revision is None:
        raise ValueError("AI intake canary scenario revision was not found")
    definition = CanaryScenarioDefinition.model_validate(revision.definition)
    policy_selection = command.policy_selection or _policy_selection_from_version(
        db, command.policy_version_id
    )
    result = run_canary_scenario(
        definition,
        policy_selection=policy_selection,
        db=db,
        suite_id=str(command.suite_id) if command.suite_id else None,
    )
    run = AiIntakeCanaryRun(
        scenario_id=scenario.id,
        scenario_revision_id=revision.id,
        suite_id=command.suite_id,
        policy_id=result.evidence.policy_id,
        policy_version_id=result.evidence.policy_version_id,
        requested_engine=result.evidence.requested_engine.value,
        actual_engine=result.evidence.actual_engine.value,
        status="passed" if result.passed else "failed",
        evidence=result.model_dump(mode="json"),
        created_by_person_id=command.actor_person_id,
    )
    db.add(run)
    db.flush()
    return RunCanaryScenarioOutcome(
        run_id=run.id,
        scenario_id=scenario.id,
        scenario_revision_id=revision.id,
        passed=result.passed,
        result=result,
    )


def _save_suite_locked(
    db: Session, command: SaveCanarySuiteCommand
) -> SaveCanarySuiteOutcome:
    suite_key = command.suite_key.strip()
    suite_name = command.name.strip()
    if not suite_key:
        raise ValueError("Canary suite key is required")
    if not suite_name:
        raise ValueError("Canary suite name is required")
    scenario_ids = tuple(dict.fromkeys(command.scenario_ids))
    suite = (
        db.query(AiIntakeCanarySuite)
        .filter(AiIntakeCanarySuite.suite_key == suite_key)
        .with_for_update()
        .one_or_none()
    )
    if suite is None:
        suite = AiIntakeCanarySuite(
            suite_key=suite_key,
            name=suite_name,
            description=command.description,
            enabled=command.enabled,
            required_for_activation=command.required_for_activation,
            created_by_person_id=command.actor_person_id,
            updated_by_person_id=command.actor_person_id,
        )
        db.add(suite)
        db.flush()
    else:
        suite.name = suite_name
        suite.description = command.description
        suite.enabled = command.enabled
        suite.required_for_activation = command.required_for_activation
        suite.updated_by_person_id = command.actor_person_id
    if scenario_ids:
        existing = (
            db.query(AiIntakeCanarySuiteScenario)
            .filter(AiIntakeCanarySuiteScenario.suite_id == suite.id)
            .all()
        )
        for row in existing:
            db.delete(row)
        db.flush()
        valid_scenarios = {
            row.id
            for row in db.query(AiIntakeCanaryScenario)
            .filter(AiIntakeCanaryScenario.id.in_(scenario_ids))
            .all()
        }
        for position, scenario_id in enumerate(scenario_ids):
            if scenario_id not in valid_scenarios:
                raise ValueError("Canary suite references an unknown scenario")
            db.add(
                AiIntakeCanarySuiteScenario(
                    suite_id=suite.id,
                    scenario_id=scenario_id,
                    position=position,
                )
            )
    db.flush()
    return SaveCanarySuiteOutcome(
        suite_id=suite.id,
        suite_key=suite.suite_key,
        scenario_count=len(scenario_ids),
    )


def _run_suite_locked(
    db: Session, command: RunCanarySuiteCommand
) -> RunCanarySuiteOutcome:
    suite = db.get(AiIntakeCanarySuite, command.suite_id)
    if suite is None:
        raise ValueError("AI intake canary suite was not found")
    links = (
        db.query(AiIntakeCanarySuiteScenario)
        .filter(AiIntakeCanarySuiteScenario.suite_id == suite.id)
        .order_by(AiIntakeCanarySuiteScenario.position.asc())
        .all()
    )
    results: list[RunCanaryScenarioOutcome] = []
    for link in links:
        results.append(
            _run_persisted_scenario_locked(
                db,
                RunCanaryScenarioCommand(
                    context=command.context,
                    scenario_id=link.scenario_id,
                    policy_version_id=command.policy_version_id,
                    policy_selection=command.policy_selection,
                    suite_id=suite.id,
                    actor_person_id=command.actor_person_id,
                ),
            )
        )
    return RunCanarySuiteOutcome(
        suite_id=suite.id,
        run_ids=tuple(result.run_id for result in results),
        passed=all(result.passed for result in results),
        scenario_results=tuple(results),
    )


def _policy_selection_from_version(
    db: Session, policy_version_id: UUID | None
) -> CanaryPolicySelection:
    if policy_version_id is None:
        return CanaryPolicySelection()
    version = db.get(AiIntakePolicyVersion, policy_version_id)
    if version is None:
        raise ValueError("AI intake policy version was not found")
    metadata = dict(version.metadata_ or {})
    templates = metadata.get("conversation_templates")
    template_map = templates if isinstance(templates, dict) else {}
    engine = metadata.get("conversation_engine_mode") or metadata.get("engine_mode")
    requested_engine = (
        CanaryEngineMode(engine)
        if engine in {mode.value for mode in CanaryEngineMode}
        else CanaryEngineMode.langgraph_v1
    )
    return CanaryPolicySelection(
        policy_id=version.policy_id,
        policy_version_id=version.id,
        policy_version_number=version.version_number,
        requested_engine=requested_engine,
        support_identity=version.display_name,
        welcome_message=version.welcome_message,
        standard_handoff_message=str(
            template_map.get("standard_handoff")
            or CanaryPolicySelection().standard_handoff_message
        ),
        media_handoff_message=str(
            template_map.get("media_first_handoff")
            or CanaryPolicySelection().media_handoff_message
        ),
    )


def _payload_sha256(payload: dict[str, object]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
