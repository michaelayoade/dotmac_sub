"""Non-authoritative SQLite behavior tests; no deployed-schema/locking claims."""

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models.audit import AuditEvent
from app.models.dispatch import WorkOrderAssignmentQueue
from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.models.work_order import WorkOrder
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services import project_work_order_handoff as owner
from app.services import staff_permission_admission
from app.services.audit_adapter import AuditRecord
from app.services.domain_errors import DomainError
from app.services.events import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    CurrentEngineeringPlanApproval,
    EngineeringCriterion,
    EngineeringPlanContent,
    EngineeringTechnology,
    GeoPoint,
    IssuanceEngineeringPlanQuery,
    SurveyedPlanLocation,
)
from app.services.project_work_order_handoff_contracts import (
    PERMISSION,
    IssueProjectWorkOrder,
)
from app.services.projects import ProjectEngineeringOrigin
from app.services.staff_permission_admission import AuthorizedStaffPerson
from app.services.work_order_assignment_contracts import CurrentWorkOrderAssignmentQuery
from app.services.work_order_commands import work_order_commands
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
)


@dataclass(kw_only=True)
class HandoffRig:
    db: Session
    approval: CurrentEngineeringPlanApproval
    origin: ProjectEngineeringOrigin
    command: IssueProjectWorkOrder
    denied: bool = False
    events: list[EventType] = field(default_factory=list)
    calls: int = 0


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[HandoffRig]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkOrder.__table__,
            ProjectWorkOrderIssuance.__table__,
            WorkOrderAssignmentQueue.__table__,
        ],
    )
    db = Session(engine)
    actor, person, project, task = (uuid4() for _ in range(4))
    now = datetime.now(UTC)
    target = InfrastructureReference(type=InfrastructureType.cabinet, id=uuid4())
    location = SurveyedPlanLocation(
        description="Reviewed site",
        point=GeoPoint(latitude=Decimal("9"), longitude=Decimal("7")),
    )
    approval = CurrentEngineeringPlanApproval(
        plan_id=uuid4(),
        project_id=project,
        task_id=task,
        revision_id=uuid4(),
        approval_id=uuid4(),
        policy_revision_id=uuid4(),
        digest="a" * 64,
        content=EngineeringPlanContent(
            technology=EngineeringTechnology.fiber,
            infrastructure=target,
            location=location,
            scope_statement="Install approved scope",
            criteria=(
                EngineeringCriterion(
                    id=uuid4(),
                    order=0,
                    statement="As reviewed",
                    verification_method="Inspect",
                ),
            ),
        ),
        author_person_id=uuid4(),
        reviewer_person_id=uuid4(),
        generation=1,
    )
    origin = ProjectEngineeringOrigin(
        project_id=project,
        task_id=task,
        project_updated_at=now,
        task_updated_at=now,
        project_template_id=None,
        applied_template_revision=None,
        task_template_id=None,
        task_template_revision=None,
        infrastructure=target,
        issuance_eligible=True,
    )
    context = CommandContext(
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor=str(actor),
        scope=PERMISSION,
        reason="Approved rollout",
        idempotency_key="issuance-test",
    )
    command = IssueProjectWorkOrder(
        context=context,
        access=PermissionAdmission(actor, "system_user", frozenset({PERMISSION})),
        project_id=project,
        task_id=task,
        expected_project_updated_at=now,
        expected_task_updated_at=now,
        plan_id=approval.plan_id,
        revision_id=approval.revision_id,
        approval_id=approval.approval_id,
        digest=approval.digest,
        infrastructure=target,
        location=location,
        classification=ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
            access_technology=WorkOrderAccessTechnology.FIBER,
        ),
        title="Approved project work",
        description=None,
        priority="normal",
    )
    state = HandoffRig(db=db, approval=approval, origin=origin, command=command)

    def authorize(
        _db: Session, *, admission: PermissionAdmission, permission: str, write: bool
    ) -> AuthorizedStaffPerson:
        assert permission == PERMISSION and write
        state.calls += 1
        if state.denied:
            raise owner._error("denied")
        return AuthorizedStaffPerson(actor, person)

    def native_origin(
        _db: Session, *, project_id: UUID, task_id: UUID | None, lock: bool
    ) -> ProjectEngineeringOrigin:
        assert lock and project_id == state.origin.project_id
        assert task_id == state.origin.task_id
        return state.origin

    def resolve(
        _db: Session, *, query: IssuanceEngineeringPlanQuery
    ) -> CurrentEngineeringPlanApproval:
        if (
            query.expected_approval_id != state.approval.approval_id
            or query.expected_revision_id != state.approval.revision_id
            or query.expected_digest != state.approval.digest
        ):
            raise owner._error("stale_state")
        return state.approval

    def audit(_db: Session, record: AuditRecord) -> AuditEvent:
        return AuditEvent(
            action=record.action,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
        )

    def emit(
        _db: Session,
        event_type: EventType,
        payload: dict[str, object],
        *,
        actor: str | None,
    ) -> Event:
        state.events.append(event_type)
        return Event(event_type=event_type, payload=payload, actor=actor)

    monkeypatch.setattr(owner, "revalidate_staff_admission", authorize)
    monkeypatch.setattr(
        staff_permission_admission, "revalidate_staff_admission", authorize
    )
    monkeypatch.setattr(owner, "engineering_plan_origin", native_origin)
    monkeypatch.setattr(owner, "stage_current_for_issuance", resolve)
    monkeypatch.setattr(owner.audit_adapter, "stage", audit)
    monkeypatch.setattr(owner, "emit_event", emit)
    yield state
    db.close()
    engine.dispose()


@pytest.mark.parametrize("technology", ["fiber", "radio"])
def test_issue_exact_provenance_without_customer_or_ticket(
    rig: HandoffRig, technology: str
) -> None:
    rig.approval = replace(
        rig.approval,
        content=rig.approval.content.model_copy(
            update={"technology": EngineeringTechnology(technology)}
        ),
    )
    command = replace(
        rig.command,
        classification=ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
            access_technology=WorkOrderAccessTechnology(technology),
        ),
    )
    outcome = owner.issue(rig.db, command=command)
    row = rig.db.get(WorkOrder, outcome.work_order_id)
    assert row is not None
    assert row.subscriber_id is None and row.origin_ticket_id is None
    assert (
        row.project_id == command.project_id and row.project_task_id == command.task_id
    )
    assert row.status == "draft" and row.work_order_kind == "infrastructure"
    assert (
        work_order_commands.current_assignment(
            rig.db, query=CurrentWorkOrderAssignmentQuery(row.id)
        )
        is None
    )
    assert row.access_technology == technology
    assert (
        outcome.approval_id == command.approval_id and outcome.digest == command.digest
    )
    assert len(rig.events) == 1


def test_replay_historical_receipt_after_origin_held(rig: HandoffRig) -> None:
    first = owner.issue(rig.db, command=rig.command)
    rig.origin = replace(rig.origin, issuance_eligible=False)
    replay = owner.issue(
        rig.db,
        command=replace(
            rig.command, context=replace(rig.command.context, command_id=uuid4())
        ),
    )
    assert replay.replayed and replay.work_order_id == first.work_order_id
    assert replay.command_id == first.command_id and len(rig.events) == 1
    rig.denied = True
    with pytest.raises(DomainError, match="denied"):
        owner.issue(rig.db, command=rig.command)


def test_conflicting_retry_refused(rig: HandoffRig) -> None:
    owner.issue(rig.db, command=rig.command)
    with pytest.raises(DomainError, match="idempotency conflict"):
        owner.issue(rig.db, command=replace(rig.command, title="Different"))


@pytest.mark.parametrize(
    "change", ["held", "preview", "approval", "digest", "location", "technology"]
)
def test_stale_or_mismatched_inputs_create_nothing(
    rig: HandoffRig, change: str
) -> None:
    command = rig.command
    if change == "held":
        rig.origin = replace(rig.origin, issuance_eligible=False)
    elif change == "preview":
        command = replace(
            command, expected_project_updated_at=datetime(2020, 1, 1, tzinfo=UTC)
        )
    elif change == "approval":
        command = replace(command, approval_id=uuid4())
    elif change == "digest":
        command = replace(command, digest="b" * 64)
    elif change == "location":
        command = replace(
            command,
            location=command.location.model_copy(update={"description": "Other"}),
        )
    else:
        command = replace(
            command,
            classification=ExplicitWorkOrderClassification(
                purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
                access_technology=WorkOrderAccessTechnology.RADIO,
            ),
        )
    with pytest.raises(DomainError):
        owner.issue(rig.db, command=command)
    assert rig.db.scalar(select(WorkOrder.id)) is None and not rig.events


def test_participant_failure_rolls_back_receipt_and_header(
    rig: HandoffRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(
        _db: Session,
        event_type: EventType,
        payload: dict[str, object],
        *,
        actor: str | None,
    ) -> Event:
        raise RuntimeError("outbox failure")

    monkeypatch.setattr(owner, "emit_event", fail)
    with pytest.raises(RuntimeError, match="outbox failure"):
        owner.issue(rig.db, command=rig.command)
    assert rig.db.scalar(select(WorkOrder.id)) is None
    assert rig.db.scalar(select(ProjectWorkOrderIssuance.id)) is None


def test_project_only_issuance_is_explicit(rig: HandoffRig) -> None:
    rig.origin = replace(rig.origin, task_id=None, task_updated_at=None)
    rig.approval = replace(rig.approval, task_id=None)
    command = replace(rig.command, task_id=None, expected_task_updated_at=None)
    outcome = owner.issue(rig.db, command=command)
    assert outcome.task_id is None
    row = rig.db.get(WorkOrder, outcome.work_order_id)
    assert row is not None and row.project_task_id is None


def test_changed_staff_person_after_lock_wait_refuses_issue(
    rig: HandoffRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def changing_authorization(
        _db: Session, *, admission: PermissionAdmission, permission: str, write: bool
    ) -> AuthorizedStaffPerson:
        nonlocal calls
        calls += 1
        return AuthorizedStaffPerson(
            admission.principal_id,
            rig.approval.author_person_id
            if calls == 1
            else rig.approval.reviewer_person_id,
        )

    monkeypatch.setattr(owner, "revalidate_staff_admission", changing_authorization)
    with pytest.raises(DomainError, match="denied"):
        owner.issue(rig.db, command=rig.command)
    assert rig.db.scalar(select(WorkOrder.id)) is None


def test_receipt_is_immutable(rig: HandoffRig) -> None:
    issued = owner.issue(rig.db, command=rig.command)
    row = rig.db.get(ProjectWorkOrderIssuance, issued.issuance_id)
    assert row is not None
    row.reason = "Rewritten"
    with pytest.raises(ValueError, match="immutable"):
        rig.db.flush()
    rig.db.rollback()
