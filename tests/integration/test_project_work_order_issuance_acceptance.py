"""Authored acceptance source; coordinator executes on real migrated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.orm import Session, SessionTransaction

from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.models.rbac import Permission, SystemUserPermission
from app.models.work_order import WorkOrder
from app.services import project_engineering_plans as approval_owner
from app.services import project_work_order_handoff as owner
from app.services.events import Event, EventType
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    EngineeringCriterion,
    EngineeringPlanOutcome,
)
from app.services.project_work_order_handoff_contracts import (
    PERMISSION,
    IssueProjectWorkOrder,
    ProjectWorkOrderIssued,
)
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
)
from tests.integration.test_project_engineering_plan_acceptance import (  # noqa: F401
    NativeScope,
    prepare,
    review,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as engineering_native_engine,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    scope as engineering_scope,
)

native_engine = engineering_native_engine
scope = engineering_scope


def approved_command(engine: Engine, origin: NativeScope) -> IssueProjectWorkOrder:
    configured, draft, revision = prepare(engine, origin)
    with Session(engine) as db:
        approved = approval_owner.decide(
            db, command=review(origin, configured, revision)
        )
        permission = db.scalar(
            select(Permission).where(
                Permission.key == PERMISSION, Permission.is_active.is_(True)
            )
        )
        assert permission is not None
        db.add(
            SystemUserPermission(
                system_user_id=origin.reviewer_id, permission_id=permission.id
            )
        )
        db.commit()
    assert approved.plan_id is not None
    assert approved.revision_id is not None
    assert approved.decision_id is not None
    assert approved.digest is not None
    return IssueProjectWorkOrder(
        context=replace(
            draft.context,
            actor=str(origin.reviewer_id),
            scope=PERMISSION,
            command_id=uuid4(),
            idempotency_key=str(uuid4()),
        ),
        access=PermissionAdmission(
            origin.reviewer_id, "system_user", frozenset({PERMISSION})
        ),
        project_id=origin.project_id,
        task_id=origin.task_id,
        expected_project_updated_at=origin.project_token,
        expected_task_updated_at=origin.task_token,
        plan_id=approved.plan_id,
        revision_id=approved.revision_id,
        approval_id=approved.decision_id,
        digest=approved.digest,
        infrastructure=draft.content.infrastructure,
        location=draft.content.location,
        classification=ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
            access_technology=WorkOrderAccessTechnology.FIBER,
        ),
        title="Approved native PON work",
        description=None,
        priority="normal",
    )


def test_duplicate_issuance_is_one_historical_receipt(
    native_engine: Engine, scope: NativeScope
) -> None:
    command = approved_command(native_engine, scope)
    gate = Barrier(2)

    def issue() -> ProjectWorkOrderIssued:
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            return owner.issue(db, command=command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(issue) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert results[0].work_order_id == results[1].work_order_id
    assert sorted(result.replayed for result in results) == [False, True]
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProjectWorkOrderIssuance)
                .where(
                    ProjectWorkOrderIssuance.idempotency_key
                    == command.context.idempotency_key
                )
            )
            == 1
        )
        row = db.get(WorkOrder, results[0].work_order_id)
        assert row is not None
        assert row.subscriber_id is None and row.origin_ticket_id is None


def test_outbox_failure_rolls_back_canonical_create(
    native_engine: Engine, scope: NativeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = approved_command(native_engine, scope)

    def fail(
        _db: Session,
        event_type: EventType,
        payload: dict[str, object],
        *,
        actor: str | None,
    ) -> Event:
        raise RuntimeError("Injected outbox failure")

    monkeypatch.setattr(owner, "emit_event", fail)
    with Session(native_engine) as db:
        with pytest.raises(RuntimeError, match="outbox failure"):
            owner.issue(db, command=command)
        assert (
            db.scalar(
                select(ProjectWorkOrderIssuance.id).where(
                    ProjectWorkOrderIssuance.idempotency_key
                    == command.context.idempotency_key
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


def test_revocation_versus_issuance_serializes_current_head(
    native_engine: Engine, scope: NativeScope
) -> None:
    from app.models.project_engineering_plan import (
        EngineeringPlanDecision,
        ProjectEngineeringPlan,
    )
    from app.services.project_engineering_plan_contracts import (
        DecideEngineeringPlan,
        PlanDecisionKind,
    )
    from tests.integration.test_project_engineering_plan_acceptance import (
        admission,
        context,
    )

    command = approved_command(native_engine, scope)
    with Session(native_engine) as db:
        head = db.get(ProjectEngineeringPlan, command.plan_id)
        decision = db.get(EngineeringPlanDecision, command.approval_id)
        assert head is not None and decision is not None
        revoke = DecideEngineeringPlan(
            context=context(scope.reviewer_id),
            access=admission(scope.reviewer_id),
            project_id=scope.project_id,
            task_id=scope.task_id,
            expected_project_updated_at=scope.project_token,
            expected_task_updated_at=scope.task_token,
            plan_id=command.plan_id,
            expected_generation=head.generation,
            revision_id=command.revision_id,
            expected_digest=command.digest,
            expected_policy_revision_id=decision.policy_revision_id,
            decision=PlanDecisionKind.revoke,
        )
    barrier = Barrier(2)

    def issue() -> ProjectWorkOrderIssued | None:
        with Session(native_engine) as db:
            barrier.wait(timeout=10)
            try:
                return owner.issue(db, command=command)
            except approval_owner.EngineeringPlanError as error:
                assert error.code.endswith("unapproved")
                return None

    def revoke_current() -> EngineeringPlanOutcome:
        with Session(native_engine) as db:
            barrier.wait(timeout=10)
            return approval_owner.decide(db, command=revoke)

    with ThreadPoolExecutor(max_workers=2) as executor:
        issued_future = executor.submit(issue)
        revoked_future = executor.submit(revoke_current)
        issued = issued_future.result(timeout=30)
        revoked_future.result(timeout=30)
    with Session(native_engine) as db:
        current = db.get(ProjectEngineeringPlan, command.plan_id)
        assert current is not None and current.current_approval_id is None
        db.rollback()
        with pytest.raises(approval_owner.EngineeringPlanError):
            owner.issue(
                db,
                command=replace(
                    command,
                    context=replace(command.context, idempotency_key=str(uuid4())),
                ),
            )
        if issued is not None:
            replay = owner.issue(db, command=command)
            assert replay.replayed and replay.work_order_id == issued.work_order_id


def test_database_receipt_rejects_rewriting(
    native_engine: Engine, scope: NativeScope
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    command = approved_command(native_engine, scope)
    with Session(native_engine) as db:
        result = owner.issue(db, command=command)
        with pytest.raises(DBAPIError, match="immutable"):
            db.execute(
                text(
                    "UPDATE project_work_order_issuances SET reason='changed' WHERE id=:id"
                ),
                {"id": result.issuance_id},
            )
        db.rollback()
        row = db.get(ProjectWorkOrderIssuance, result.issuance_id)
        assert row is not None and row.reason == command.context.reason


def test_project_lock_precedes_task_and_held_state_is_refreshed(
    native_engine: Engine, scope: NativeScope
) -> None:
    from threading import Event
    from time import monotonic

    from sqlalchemy import event, text

    from app.models.project import Project, ProjectTask
    from app.services.domain_errors import DomainError

    command = approved_command(native_engine, scope)
    ready = Event()
    worker_pid: list[int] = []

    def issue_waiting() -> None:
        with Session(native_engine) as db:

            def began(
                _session: Session,
                _transaction: SessionTransaction,
                connection: Connection,
            ) -> None:
                pid = connection.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(pid, int)
                worker_pid.append(pid)
                ready.set()

            event.listen(db, "after_begin", began, once=True)
            with pytest.raises(DomainError):
                owner.issue(db, command=command)

    with (
        Session(native_engine) as blocker,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        project = blocker.scalar(
            select(Project).where(Project.id == scope.project_id).with_for_update()
        )
        future = executor.submit(issue_waiting)
        assert ready.wait(timeout=10)
        deadline = monotonic() + 10
        waiting = False
        while monotonic() < deadline:
            with native_engine.connect() as observer:
                waiting = bool(
                    observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE pid=:pid AND wait_event_type='Lock'"
                        ),
                        {"pid": worker_pid[0]},
                    )
                )
            if waiting:
                break
        assert waiting, "Issuance must wait for the authoritative Project lock"
        # The waiting issuer has not taken the task lock ahead of Project.
        blocker.scalar(
            select(ProjectTask)
            .where(ProjectTask.id == scope.task_id)
            .with_for_update(nowait=True)
        )
        assert project is not None
        project.status = "on_hold"
        blocker.commit()
        future.result(timeout=30)
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(ProjectWorkOrderIssuance.id).where(
                    ProjectWorkOrderIssuance.idempotency_key
                    == command.context.idempotency_key
                )
            )
            is None
        )


def test_supersession_versus_issuance_has_one_exact_approval(
    native_engine: Engine, scope: NativeScope
) -> None:
    from app.models.project_engineering_plan import (
        EngineeringPlanCriterion,
        EngineeringPlanDecision,
        EngineeringPlanRevision,
        ProjectEngineeringPlan,
    )
    from app.services.project_engineering_plan_contracts import (
        DecideEngineeringPlan,
        DraftEngineeringPlan,
        EngineeringPlanContent,
        PlanDecisionKind,
    )
    from tests.integration.test_project_engineering_plan_acceptance import (
        admission,
        context,
    )

    command = approved_command(native_engine, scope)
    with Session(native_engine) as db:
        head = db.get(ProjectEngineeringPlan, command.plan_id)
        prior = db.get(EngineeringPlanDecision, command.approval_id)
        revision = db.get(EngineeringPlanRevision, command.revision_id)
        assert head is not None and prior is not None and revision is not None
        criteria = tuple(
            EngineeringCriterion(
                id=row.id,
                order=row.order,
                statement=row.statement,
                verification_method=row.verification_method,
            )
            for row in db.scalars(
                select(EngineeringPlanCriterion)
                .where(EngineeringPlanCriterion.revision_id == revision.id)
                .order_by(EngineeringPlanCriterion.order)
            )
        )
        content = EngineeringPlanContent.model_validate(
            {**revision.content, "criteria": criteria}
        )
        generation, policy_id = head.generation, prior.policy_revision_id
        db.rollback()
        draft = approval_owner.draft_revision(
            db,
            command=DraftEngineeringPlan(
                context=context(scope.author_id),
                access=admission(scope.author_id),
                project_id=scope.project_id,
                task_id=scope.task_id,
                expected_project_updated_at=scope.project_token,
                expected_task_updated_at=scope.task_token,
                plan_id=command.plan_id,
                expected_generation=generation,
                content=content.model_copy(
                    update={"scope_statement": "New reviewed scope"}
                ),
            ),
        )
    assert draft.revision_id is not None and draft.digest is not None
    decision = DecideEngineeringPlan(
        context=context(scope.reviewer_id),
        access=admission(scope.reviewer_id),
        project_id=scope.project_id,
        task_id=scope.task_id,
        expected_project_updated_at=scope.project_token,
        expected_task_updated_at=scope.task_token,
        plan_id=command.plan_id,
        expected_generation=draft.generation,
        revision_id=draft.revision_id,
        expected_digest=draft.digest,
        expected_policy_revision_id=policy_id,
        decision=PlanDecisionKind.approve,
    )
    gate = Barrier(2)

    def issue() -> ProjectWorkOrderIssued | None:
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            try:
                return owner.issue(db, command=command)
            except approval_owner.EngineeringPlanError as error:
                assert error.code.endswith("unapproved")
                return None

    def supersede() -> EngineeringPlanOutcome:
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            return approval_owner.decide(db, command=decision)

    with ThreadPoolExecutor(max_workers=2) as executor:
        issued_future, approved_future = (
            executor.submit(issue),
            executor.submit(supersede),
        )
        issued, approved = (
            issued_future.result(timeout=30),
            approved_future.result(timeout=30),
        )
    with Session(native_engine) as db:
        current = db.get(ProjectEngineeringPlan, command.plan_id)
        assert (
            current is not None and current.current_approval_id == approved.decision_id
        )
        db.rollback()
        with pytest.raises(approval_owner.EngineeringPlanError):
            owner.issue(
                db,
                command=replace(
                    command,
                    context=replace(command.context, idempotency_key=str(uuid4())),
                ),
            )
        if issued:
            receipt = db.get(ProjectWorkOrderIssuance, issued.issuance_id)
            assert receipt is not None
            assert (
                receipt.approval_id == command.approval_id
                and receipt.digest == command.digest
            )
