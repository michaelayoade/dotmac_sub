"""Dedicated-server acceptance only: migrated private clones, real native admission.

These tests are authored here but must be executed by the coordinator on the
approved PostgreSQL server. No metadata synthesis, SQLite or mocked admission.
"""

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, insert, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.network import OLTDevice, PonPort
from app.models.project import (
    Project,
    ProjectInfrastructure,
    ProjectStatus,
    ProjectTask,
    ProjectTaskStatus,
    ProjectType,
)
from app.models.project_engineering_plan import (
    EngineeringPlanDecision,
    EngineeringPlanReceipt,
    EngineeringPlanRevision,
    ProjectEngineeringPlan,
)
from app.models.rbac import Permission, SystemUserPermission
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.schemas.project import ProjectCreate, ProjectTaskCreate
from app.services import project_engineering_plans as owner
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    CurrentEngineeringPlanQuery,
    DecideEngineeringPlan,
    DraftEngineeringPlan,
    EngineeringCriterion,
    EngineeringPlanContent,
    EngineeringPlanOutcome,
    EngineeringTechnology,
    GeoPoint,
    PlanDecisionKind,
    PublishApprovalPolicy,
    SurveyedPlanLocation,
)
from app.services.projects import project_tasks, projects
from tests.staff_identity_fixtures import add_bound_staff_user

PERMISSIONS = frozenset(
    {owner.READ, owner.WRITE, owner.APPROVE, owner.REVOKE, owner.POLICY}
)


@dataclass(frozen=True)
class NativeScope:
    project_id: UUID
    task_id: UUID
    project_token: datetime
    task_token: datetime
    author_id: UUID
    reviewer_id: UUID
    port_id: UUID
    parent_id: UUID
    other_parent_id: UUID


def admission(actor: UUID) -> PermissionAdmission:
    return PermissionAdmission(actor, "system_user", PERMISSIONS)


def context(actor: UUID) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(actor),
        scope=owner.SCOPE,
        reason="Synthetic migrated engineering acceptance",
        idempotency_key=str(uuid4()),
    )


@pytest.fixture
def native_engine(cloned_database: Callable[[str], URL]) -> Iterator[Engine]:
    engine = create_engine(
        cloned_database("heads"),
        connect_args={"options": "-c statement_timeout=15000 -c lock_timeout=10000"},
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def scope(native_engine: Engine) -> NativeScope:
    with Session(native_engine, expire_on_commit=False) as db:
        author, _ = add_bound_staff_user(db)
        reviewer, _ = add_bound_staff_user(db)
        permissions = db.scalars(
            select(Permission).where(
                Permission.key.in_(PERMISSIONS), Permission.is_active.is_(True)
            )
        ).all()
        assert {p.key for p in permissions} == PERMISSIONS, (
            "Migration must catalogue permissions without implicit grants"
        )
        for user in (author, reviewer):
            for permission in permissions:
                db.add(
                    SystemUserPermission(
                        system_user_id=user.id, permission_id=permission.id
                    )
                )
        parent = OLTDevice(
            name="Reviewed native OLT",
            vendor="Huawei",
            is_active=True,
            config_pack={
                "internet_vlan_id": 203,
                "management_vlan_id": 201,
                "tr069_olt_profile_id": 2,
            },
        )
        other = OLTDevice(
            name="Alternative native OLT",
            vendor="Huawei",
            is_active=True,
            config_pack={
                "internet_vlan_id": 203,
                "management_vlan_id": 201,
                "tr069_olt_profile_id": 2,
            },
        )
        db.add_all([parent, other])
        db.flush()
        port = PonPort(olt_id=parent.id, name="Synthetic PON", is_active=True)
        db.add(port)
        db.flush()
        author_id, reviewer_id, port_id, parent_id, other_id = (
            author.id,
            reviewer.id,
            port.id,
            parent.id,
            other.id,
        )
        db.commit()
    # Use the same typed physical-project entry point available to staff. The
    # native inventory is committed first; no NULL-typed project/link shortcut.
    with Session(native_engine) as db:
        project = projects.create(
            db,
            ProjectCreate(
                name="Synthetic engineering acceptance",
                status=ProjectStatus.active,
                project_type=ProjectType.fiber_optics_installation,
                infrastructure=InfrastructureReference(
                    type=InfrastructureType.pon_port, id=port_id
                ),
            ),
        )
        task = project_tasks.create(
            db,
            ProjectTaskCreate(
                project_id=project.id,
                title="Reviewed engineering task",
                status=ProjectTaskStatus.todo,
            ),
        )
        return NativeScope(
            project.id,
            task.id,
            project.updated_at,
            task.updated_at,
            author_id,
            reviewer_id,
            port_id,
            parent_id,
            other_id,
        )


def prepare(
    engine: Engine,
    scope: NativeScope,
    *,
    content: EngineeringPlanContent | None = None,
) -> tuple[EngineeringPlanOutcome, DraftEngineeringPlan, EngineeringPlanOutcome]:
    with Session(engine) as db:
        configured = owner.publish_policy(
            db,
            command=PublishApprovalPolicy(
                context=context(scope.reviewer_id),
                access=admission(scope.reviewer_id),
                project_id=scope.project_id,
                expected_project_updated_at=scope.project_token,
                expected_generation=0,
                independent_reviewer_required=True,
            ),
        )
        draft_command = DraftEngineeringPlan(
            context=context(scope.author_id),
            access=admission(scope.author_id),
            plan_id=uuid4(),
            project_id=scope.project_id,
            task_id=scope.task_id,
            expected_project_updated_at=scope.project_token,
            expected_task_updated_at=scope.task_token,
            expected_generation=0,
            content=content
            or EngineeringPlanContent(
                technology=EngineeringTechnology.fiber,
                infrastructure=InfrastructureReference(
                    type=InfrastructureType.pon_port, id=scope.port_id
                ),
                location=SurveyedPlanLocation(
                    description="Reviewed native PON survey",
                    point=GeoPoint(latitude=Decimal("9.1"), longitude=Decimal("7.2")),
                ),
                scope_statement="Reviewed existing PON engineering scope",
                criteria=(
                    EngineeringCriterion(
                        id=uuid4(),
                        order=0,
                        statement="Conform to reviewed design",
                        verification_method="Review measured survey",
                    ),
                ),
            ),
        )
        drafted = owner.draft_revision(db, command=draft_command)
    return configured, draft_command, drafted


def review(
    scope: NativeScope,
    configured: EngineeringPlanOutcome,
    revision: EngineeringPlanOutcome,
    kind: PlanDecisionKind = PlanDecisionKind.approve,
    generation: int | None = None,
) -> DecideEngineeringPlan:
    return DecideEngineeringPlan(
        context=context(scope.reviewer_id),
        access=admission(scope.reviewer_id),
        plan_id=revision.plan_id,
        project_id=scope.project_id,
        task_id=scope.task_id,
        expected_project_updated_at=scope.project_token,
        expected_task_updated_at=scope.task_token,
        expected_generation=revision.generation if generation is None else generation,
        revision_id=revision.revision_id,
        expected_digest=revision.digest,
        expected_policy_revision_id=configured.policy_revision_id,
        decision=kind,
    )


def query(
    scope: NativeScope, approved: EngineeringPlanOutcome
) -> CurrentEngineeringPlanQuery:
    return CurrentEngineeringPlanQuery(
        access=admission(scope.reviewer_id),
        plan_id=approved.plan_id,
        project_id=scope.project_id,
        task_id=scope.task_id,
        expected_revision_id=approved.revision_id,
        expected_approval_id=approved.decision_id,
        expected_digest=approved.digest,
    )


def race(
    engine: Engine, commands: Sequence[DecideEngineeringPlan]
) -> list[EngineeringPlanOutcome | owner.EngineeringPlanError]:
    barrier = Barrier(len(commands))

    def execute(
        command: DecideEngineeringPlan,
    ) -> EngineeringPlanOutcome | owner.EngineeringPlanError:
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                return owner.decide(db, command=command)
            except owner.EngineeringPlanError as exc:
                return exc

    with ThreadPoolExecutor(max_workers=len(commands)) as workers:
        futures = [workers.submit(execute, command) for command in commands]
        return [future.result(timeout=30) for future in futures]


def test_approval_revoke_atomic_receipts_audit_outbox(
    native_engine: Engine, scope: NativeScope
) -> None:
    configured, _, drafted = prepare(native_engine, scope)
    approve = review(scope, configured, drafted)
    with Session(native_engine) as db:
        approved = owner.decide(db, command=approve)
        assert (
            owner.resolve_current(db, query=query(scope, approved)).approval_id
            == approved.decision_id
        )
        db.rollback()
        revoke = review(scope, configured, approved, PlanDecisionKind.revoke)
        revoked = owner.decide(db, command=revoke)
        assert owner.decide(db, command=approve).replayed
        with pytest.raises(owner.EngineeringPlanError):
            owner.resolve_current(db, query=query(scope, approved))
    with Session(native_engine) as db:
        for command in (approve, revoke):
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(EngineeringPlanReceipt)
                    .where(
                        EngineeringPlanReceipt.idempotency_key
                        == command.context.idempotency_key
                    )
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.action == "project_engineering_plan.changed",
                        AuditEvent.metadata_["command_id"].as_string()
                        == str(command.context.command_id),
                    )
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(EventStore)
                    .where(
                        EventStore.event_type == "project_engineering_plan.changed",
                        EventStore.payload["command_id"].as_string()
                        == str(command.context.command_id),
                    )
                )
                == 1
            )
        assert (
            db.get(ProjectEngineeringPlan, approved.plan_id).current_approval_id is None
        )
        assert revoked.generation == approved.generation + 1


@pytest.mark.parametrize(
    ("project_status", "task_status"),
    [
        (ProjectStatus.on_hold, None),
        (None, ProjectTaskStatus.blocked),
        (None, ProjectTaskStatus.done),
    ],
    ids=["project-on-hold", "task-blocked", "task-done"],
)
def test_approved_plan_cannot_issue_after_native_scope_status_change(
    native_engine: Engine,
    scope: NativeScope,
    project_status: ProjectStatus | None,
    task_status: ProjectTaskStatus | None,
) -> None:
    from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
    from app.models.work_order import WorkOrder
    from app.schemas.project import ProjectTaskStatusTransition, ProjectUpdate
    from app.services import project_work_order_handoff
    from app.services.domain_errors import DomainError
    from tests.integration.test_project_work_order_issuance_acceptance import (
        approved_command,
    )

    # Reuse the genuine policy/draft/independent approval and native dispatch
    # grant fixture. An existing approval must remain insufficient after a hold.
    command = approved_command(native_engine, scope)
    with Session(native_engine) as db:
        current = owner.resolve_current(
            db,
            query=CurrentEngineeringPlanQuery(
                access=admission(scope.reviewer_id),
                plan_id=command.plan_id,
                project_id=scope.project_id,
                task_id=scope.task_id,
                expected_revision_id=command.revision_id,
                expected_approval_id=command.approval_id,
                expected_digest=command.digest,
            ),
        )
        assert current.approval_id == command.approval_id
    with Session(native_engine) as db:
        if project_status is not None:
            projects.update(
                db,
                str(scope.project_id),
                ProjectUpdate(status=project_status),
                actor_id=scope.reviewer_id,
            )
        else:
            assert task_status is not None
            project_tasks.transition_status(
                db,
                str(scope.task_id),
                ProjectTaskStatusTransition(
                    expected_status=ProjectTaskStatus.todo,
                    status=task_status,
                    reason="Refuse new issuance after native task status change",
                ),
                actor_id=scope.reviewer_id,
            )
    with Session(native_engine) as db:
        project = db.get(Project, scope.project_id)
        task = db.get(ProjectTask, scope.task_id)
        head = db.get(ProjectEngineeringPlan, command.plan_id)
        assert project is not None and task is not None and head is not None
        assert project.status == (project_status or ProjectStatus.active).value
        assert task.status == (task_status or ProjectTaskStatus.todo).value
        assert head.current_approval_id == command.approval_id
        fresh = replace(
            command,
            context=replace(
                command.context,
                command_id=uuid4(),
                idempotency_key=str(uuid4()),
            ),
            expected_project_updated_at=project.updated_at,
            expected_task_updated_at=task.updated_at,
        )
        assert (
            fresh.expected_project_updated_at,
            fresh.expected_task_updated_at,
        ) != (
            command.expected_project_updated_at,
            command.expected_task_updated_at,
        )
    # Both the stale preview and a newly refreshed preview refuse. The latter
    # proves lifecycle eligibility, rather than only timestamp mismatch.
    for attempt in (command, fresh):
        with Session(native_engine) as db:
            with pytest.raises(DomainError) as refused:
                project_work_order_handoff.issue(db, command=attempt)
            assert refused.value.code == (
                "operations.project_work_order_handoff.stale_state"
            )
        with Session(native_engine) as db:
            assert (
                db.scalar(
                    select(ProjectWorkOrderIssuance.id).where(
                        ProjectWorkOrderIssuance.project_id == scope.project_id
                    )
                )
                is None
            )
            assert (
                db.scalar(
                    select(WorkOrder.id).where(WorkOrder.project_id == scope.project_id)
                )
                is None
            )
            assert (
                db.scalar(
                    select(AuditEvent.id).where(
                        AuditEvent.metadata_["command_id"].as_string()
                        == str(attempt.context.command_id)
                    )
                )
                is None
            )
            assert (
                db.scalar(
                    select(EventStore.id).where(
                        EventStore.payload["command_id"].as_string()
                        == str(attempt.context.command_id)
                    )
                )
                is None
            )


def test_pg_immutable_revision_and_cross_project_task_fk(
    native_engine: Engine, scope: NativeScope
) -> None:
    _, _, drafted = prepare(native_engine, scope)
    with Session(native_engine) as db:
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE engineering_plan_revisions SET digest = digest WHERE id = :id"
                ),
                {"id": drafted.revision_id},
            )
            db.commit()
        db.rollback()
        other = Project(name="Other native project", is_active=True)
        db.add(other)
        db.commit()
        other_id = other.id
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(
                insert(ProjectEngineeringPlan).values(
                    id=uuid4(),
                    project_id=other_id,
                    task_id=scope.task_id,
                    technology="fiber",
                    generation=0,
                )
            )
            db.commit()
        db.rollback()


def test_duplicate_key_race_replays_one_approval(
    native_engine: Engine, scope: NativeScope
) -> None:
    configured, _, drafted = prepare(native_engine, scope)
    command = review(scope, configured, drafted)
    results = race(native_engine, [command, command])
    assert not any(isinstance(result, Exception) for result in results)
    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].decision_id == results[1].decision_id
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(EngineeringPlanDecision)
                .where(EngineeringPlanDecision.plan_id == drafted.plan_id)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(EngineeringPlanReceipt)
                .where(
                    EngineeringPlanReceipt.idempotency_key
                    == command.context.idempotency_key
                )
            )
            == 1
        )


def test_current_approval_fails_after_pon_reparent(
    native_engine: Engine, scope: NativeScope
) -> None:
    configured, _, drafted = prepare(native_engine, scope)
    with Session(native_engine) as db:
        approved = owner.decide(db, command=review(scope, configured, drafted))
    with Session(native_engine) as inventory:
        inventory.get(PonPort, scope.port_id).olt_id = scope.other_parent_id
        inventory.commit()
    with Session(native_engine) as db:
        with pytest.raises(owner.EngineeringPlanError, match="stale state"):
            owner.resolve_current(db, query=query(scope, approved))
        db.rollback()
        assert db.get(EngineeringPlanRevision, drafted.revision_id).origin_binding[
            "pon_parent_olt_id"
        ] == str(scope.parent_id)


def test_archived_origin_still_allows_exact_revocation(
    native_engine: Engine, scope: NativeScope
) -> None:
    configured, _, drafted = prepare(native_engine, scope)
    with Session(native_engine) as db:
        approved = owner.decide(db, command=review(scope, configured, drafted))
    with Session(native_engine, expire_on_commit=False) as change:
        project = change.get(Project, scope.project_id)
        task = change.get(ProjectTask, scope.task_id)
        project.is_active = False
        task.is_active = False
        change.delete(change.get(ProjectInfrastructure, scope.project_id))
        change.commit()
        changed_scope = replace(
            scope, project_token=project.updated_at, task_token=task.updated_at
        )
    with Session(native_engine) as db:
        revoked = owner.decide(
            db,
            command=review(
                changed_scope, configured, approved, PlanDecisionKind.revoke
            ),
        )
        assert revoked.action.value == "revoke"


def test_superseding_approval_and_old_revocation_serialize_head(
    native_engine: Engine, scope: NativeScope
) -> None:
    configured, draft_command, drafted = prepare(native_engine, scope)
    with Session(native_engine) as db:
        approved = owner.decide(db, command=review(scope, configured, drafted))
        next_draft = owner.draft_revision(
            db,
            command=replace(
                draft_command,
                context=context(scope.author_id),
                expected_generation=approved.generation,
            ),
        )
    new_approval = review(scope, configured, next_draft)
    revoke = review(
        scope,
        configured,
        approved,
        PlanDecisionKind.revoke,
        generation=next_draft.generation,
    )
    results = race(native_engine, [new_approval, revoke])
    assert (
        sum(isinstance(result, owner.EngineeringPlanError) for result in results) == 1
    )
    with Session(native_engine) as db:
        head = db.get(ProjectEngineeringPlan, approved.plan_id)
        assert head.generation == next_draft.generation + 1
        winner = next(result for result in results if not isinstance(result, Exception))
        assert head.current_approval_id == (
            winner.decision_id if winner.action.value == "approve" else None
        )


def test_outbox_stage_failure_rolls_back_entire_approval(
    native_engine: Engine, scope: NativeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured, _, drafted = prepare(native_engine, scope)
    command = review(scope, configured, drafted)

    def fail_outbox(db: Session, *args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic outbox staging failure")

    monkeypatch.setattr(owner, "emit_event", fail_outbox)
    with Session(native_engine) as db:
        with pytest.raises(RuntimeError, match="Synthetic outbox"):
            owner.decide(db, command=command)
    with Session(native_engine) as verify:
        head = verify.get(ProjectEngineeringPlan, drafted.plan_id)
        assert head.generation == drafted.generation
        assert head.current_approval_id is None
        assert (
            verify.scalar(
                select(func.count())
                .select_from(EngineeringPlanDecision)
                .where(EngineeringPlanDecision.plan_id == drafted.plan_id)
            )
            == 0
        )
        assert (
            verify.scalar(
                select(func.count())
                .select_from(EngineeringPlanReceipt)
                .where(
                    EngineeringPlanReceipt.idempotency_key
                    == command.context.idempotency_key
                )
            )
            == 0
        )
        assert (
            verify.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "project_engineering_plan.changed",
                    AuditEvent.metadata_["command_id"].as_string()
                    == str(command.context.command_id),
                )
            )
            == 0
        )
        assert (
            verify.scalar(
                select(func.count())
                .select_from(EventStore)
                .where(
                    EventStore.payload["command_id"].as_string()
                    == str(command.context.command_id)
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    ("technology", "require_legacy_evidence"),
    [
        (EngineeringTechnology.fiber, False),
        (EngineeringTechnology.radio, False),
        (EngineeringTechnology.radio, True),
    ],
)
def test_customerless_physical_project_create_issue_accept_complete(
    native_engine: Engine,
    scope: NativeScope,
    technology: EngineeringTechnology,
    require_legacy_evidence: bool,
):
    from app.models.network_monitoring import PopSite
    from app.models.work_order import WorkOrder
    from app.services import project_work_order_handoff
    from app.services.field import completion_project_scope, completion_review
    from app.services.project_work_order_handoff_contracts import (
        PERMISSION as ISSUE_PERMISSION,
    )
    from app.services.project_work_order_handoff_contracts import (
        IssueProjectWorkOrder,
    )
    from app.services.work_order_templates import (
        ExplicitWorkOrderClassification,
        WorkOrderAccessTechnology,
        WorkOrderPurpose,
    )
    from tests.integration.test_field_completion_review_acceptance import (
        prepare as prepare_completion,
    )
    from tests.integration.test_field_completion_review_acceptance import (
        review as review_completion,
    )
    from tests.integration.test_project_field_completion_acceptance import apply_command

    reference = InfrastructureReference(
        type=InfrastructureType.pon_port, id=scope.port_id
    )
    if technology == EngineeringTechnology.radio:
        with Session(native_engine) as db:
            base = PopSite(
                name="Canonical radio project target",
                zabbix_group_id=str(uuid4().int)[:20],
                latitude=9.1,
                longitude=7.2,
                is_active=True,
            )
            db.add(base)
            db.flush()
            reference = InfrastructureReference(
                type=InfrastructureType.base_station, id=base.id
            )
            db.commit()
        with Session(native_engine) as db:
            project = projects.create(
                db,
                ProjectCreate(
                    name="Canonical customerless radio project",
                    project_type=ProjectType.air_fiber_installation,
                    status=ProjectStatus.active,
                    infrastructure=reference,
                ),
            )
            task = project_tasks.create(
                db,
                ProjectTaskCreate(
                    project_id=project.id,
                    title="Native radio engineering task",
                    status=ProjectTaskStatus.todo,
                ),
            )
            scope = replace(
                scope,
                project_id=project.id,
                task_id=task.id,
                project_token=project.updated_at,
                task_token=task.updated_at,
            )
    content = EngineeringPlanContent(
        technology=technology,
        infrastructure=reference,
        location=SurveyedPlanLocation(
            description="Canonical physical scope survey",
            point=GeoPoint(latitude=Decimal("9.1"), longitude=Decimal("7.2")),
        ),
        scope_statement="Reviewed physical project scope",
        criteria=(
            EngineeringCriterion(
                id=uuid4(),
                order=0,
                statement="Confirm exact installed scope",
                verification_method="Inspect surveyed target",
            ),
        ),
    )
    configured, draft, drafted = prepare(native_engine, scope, content=content)
    with Session(native_engine) as db:
        approved = owner.decide(db, command=review(scope, configured, drafted))
    with Session(native_engine) as db:
        permission = db.scalar(
            select(Permission).where(
                Permission.key == ISSUE_PERMISSION, Permission.is_active.is_(True)
            )
        )
        assert permission is not None
        db.add(
            SystemUserPermission(
                system_user_id=scope.reviewer_id, permission_id=permission.id
            )
        )
        db.commit()
    with Session(native_engine) as db:
        issued = project_work_order_handoff.issue(
            db,
            command=IssueProjectWorkOrder(
                context=replace(context(scope.reviewer_id), scope=ISSUE_PERMISSION),
                access=PermissionAdmission(
                    scope.reviewer_id, "system_user", frozenset({ISSUE_PERMISSION})
                ),
                project_id=scope.project_id,
                task_id=scope.task_id,
                expected_project_updated_at=scope.project_token,
                expected_task_updated_at=scope.task_token,
                plan_id=approved.plan_id,
                revision_id=approved.revision_id,
                approval_id=approved.decision_id,
                digest=approved.digest,
                infrastructure=reference,
                location=draft.content.location,
                classification=ExplicitWorkOrderClassification(
                    purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
                    access_technology=WorkOrderAccessTechnology.FIBER
                    if technology == EngineeringTechnology.fiber
                    else WorkOrderAccessTechnology.RADIO,
                ),
                title="Canonical physical installation visit",
                description=None,
                priority="normal",
            ),
        )
    prepared = prepare_completion(
        native_engine,
        scope,
        already_issued=issued,
        require_legacy_evidence=require_legacy_evidence,
    )
    if require_legacy_evidence:
        from app.models.fiber_change_request import FiberChangeRequest
        from app.models.fiber_topology_field_observation import (
            FiberTopologyFieldObservation,
        )
        from app.models.field_fiber import FieldFiberTestResult
        from app.services.field.completion_review_contracts import (
            CompletionReviewQuery,
            ReviewState,
        )
        from app.services.field.transitions import resolve_fiber_as_built_evidence

        with Session(native_engine) as db:
            work = db.get(WorkOrder, issued.work_order_id)
            assert work is not None and work.requires_as_built_evidence is True
            evidence = resolve_fiber_as_built_evidence(db, work)
            assert evidence.required is False and evidence.satisfied is True
            assert (
                db.scalar(select(func.count()).select_from(FieldFiberTestResult)) == 0
            )
            assert (
                db.scalar(
                    select(func.count()).select_from(FiberTopologyFieldObservation)
                )
                == 0
            )
            assert db.scalar(select(func.count()).select_from(FiberChangeRequest)) == 0
            current = completion_review.current(
                db,
                query=CompletionReviewQuery(
                    access=prepared.access,
                    enrollment_id=prepared.submission.enrollment_id,
                ),
            )
            assert current.state == ReviewState.legacy_not_reviewed
        assert prepared.reviewer != prepared.submission.requester_system_user_id
    with Session(native_engine) as db:
        submitted = completion_review.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        accepted = completion_review.decide(
            db, command=review_completion(prepared, submitted)
        )
    command = apply_command(native_engine, scope, prepared.reviewer)
    with Session(native_engine) as db:
        completion_project_scope.apply_accepted_project_completion(db, command=command)
    with Session(native_engine) as db:
        project = db.get(Project, scope.project_id)
        assert project.subscriber_id is None and project.status == "completed"
        assert project.infrastructure.id == reference.id
        assert project.project_type == (
            ProjectType.fiber_optics_installation.value
            if technology == EngineeringTechnology.fiber
            else ProjectType.air_fiber_installation.value
        )
        assert db.get(ProjectTask, scope.task_id).status == "todo"
        work = db.get(WorkOrder, issued.work_order_id)
        assert work.status == "completed" and work.access_technology == technology.value
        assert accepted.state.value == "accepted"
