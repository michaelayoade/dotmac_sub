"""Migration679 PostgreSQL acceptance; run only through the migrated test owner."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, local
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.field_completion_review import (
    CompletionReplacementDecision,
    CompletionReplacementReceipt,
    CompletionReviewHead,
)
from app.models.field_job_event import FieldJobEvent
from app.models.support import (
    SupportResolutionBasis,
    SupportResolutionEffectiveBasis,
    SupportResolutionReplacementLineage,
    Ticket,
    TicketAccessToken,
)
from app.models.work_order import WorkOrder
from app.services import support
from app.services.domain_errors import DomainError
from app.services.events import EventType
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    WRITE,
    CriterionCoverage,
    CriterionObservation,
    CriterionResult,
    CriterionVerdict,
    DecideCompletionReview,
    FailedScopeReplacementWorkspaceQuery,
    PublishCompletionCriteria,
    PublishCompletionReviewPolicy,
    RejectedSubmissionFailure,
    ReplacementWorkspacePurpose,
    ReviewFailedScopeReplacement,
    RevokeFailedScopeReplacement,
    SubmitCompletionEvidence,
)
from app.services.field.execution_contracts import (
    ApplyFieldTransition,
    FieldEvent,
    FieldTransitionPayload,
)
from app.services.field.transitions import field_transitions
from app.services.owner_commands import CommandContext
from app.services.work_order_assignment_contracts import WorkOrderLinkageFence
from tests.integration.test_support_completion_gate_acceptance import (
    NativeJob,
    context,
    prepare,
    request,
)
from tests.integration.test_support_completion_gate_acceptance import (
    native_engine as support_native_engine,
)

native_engine = support_native_engine


@dataclass(frozen=True, slots=True)
class FailedPair:
    source: NativeJob
    replacement: NativeJob
    command: ReviewFailedScopeReplacement


def enroll(engine: Engine, job: NativeJob) -> UUID:
    with Session(engine) as db:
        outcome = owner.enroll(db, command=job.enrollment)
    assert outcome.enrollment_id is not None
    return outcome.enrollment_id


def fail(engine: Engine, job: NativeJob) -> None:
    with Session(engine) as db:
        row = db.get(WorkOrder, job.work_order_id)
        assert row is not None
        row.status = "scheduled"
        db.commit()
    with Session(engine) as db:
        failed = field_transitions.apply(
            db,
            ApplyFieldTransition(
                context=context(job.technician_id, "field:execution"),
                requester_system_user_id=job.technician_id,
                public_id=job.public_id,
                event=FieldEvent.unable_to_complete,
                client_event_id=uuid4(),
                assignment_generation=job.assignment_generation,
                payload=FieldTransitionPayload(reason="no_access"),
            ),
        )
    assert failed.event.event == "unable_to_complete"


def command_for(
    engine: Engine, source: NativeJob, replacement: NativeJob
) -> ReviewFailedScopeReplacement:
    with Session(engine) as db:
        workspace = owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=source.access,
                work_order_public_id=source.public_id,
                purpose=ReplacementWorkspacePurpose.review,
            ),
        )
    candidate = next(
        item
        for item in workspace.candidates
        if item.work_order_public_id == replacement.public_id
    )
    assert workspace.source.failure_basis is not None
    assert (
        workspace.source.policy_revision_id is not None
        and candidate.policy_revision_id is not None
    )
    assert (
        workspace.source.assignment_generation is not None
        and candidate.assignment_generation is not None
    )
    return ReviewFailedScopeReplacement(
        context=context(source.reviewer_id, WRITE),
        access=source.access,
        source_enrollment_id=workspace.source.enrollment_id,
        replacement_enrollment_id=candidate.enrollment_id,
        expected_generation=workspace.expected_generation,
        failure_basis=workspace.source.failure_basis,
        source_criteria_revision_id=workspace.source.criteria_revision_id,
        replacement_criteria_revision_id=candidate.criteria_revision_id,
        source_policy_revision_id=workspace.source.policy_revision_id,
        replacement_policy_revision_id=candidate.policy_revision_id,
        source_assignment_generation=workspace.source.assignment_generation,
        replacement_assignment_generation=candidate.assignment_generation,
        coverage=tuple(
            CriterionCoverage(item.id, candidate.criteria[0].id)
            for item in workspace.source.criteria
        ),
        expected_work_order_public_id=source.public_id,
    )


def pair(engine: Engine) -> FailedPair:
    source = prepare(engine)
    replacement = prepare(engine, ticket_id=source.ticket_id)
    fail(engine, source)
    enroll(engine, source)
    enroll(engine, replacement)
    return FailedPair(source, replacement, command_for(engine, source, replacement))


def accept_enrolled(engine: Engine, job: NativeJob, *, passed: bool = True) -> None:
    from app.services.field.completion_review_contracts import (
        FieldSubmissionPreviewQuery,
    )

    with Session(engine) as db:
        preview = owner.field_submission_preview(
            db,
            query=FieldSubmissionPreviewQuery(
                requester_system_user_id=job.technician_id,
                work_order_public_id=job.public_id,
            ),
        )
    assert (
        preview.enrollment_id
        and preview.criteria_revision_id
        and preview.policy_revision_id
    )
    with Session(engine) as db:
        submitted = owner.submit_evidence(
            db,
            command=SubmitCompletionEvidence(
                context=context(job.technician_id, "field:completion:submit"),
                requester_system_user_id=job.technician_id,
                enrollment_id=preview.enrollment_id,
                criteria_revision_id=preview.criteria_revision_id,
                policy_revision_id=preview.policy_revision_id,
                completed_event_id=job.completed_event_id,
                assignment_generation=job.assignment_generation,
                expected_generation=preview.generation,
                observations=tuple(
                    CriterionObservation(item.id, "Measured customer repair")
                    for item in preview.criteria
                ),
            ),
        )
    assert submitted.submission_id is not None
    with Session(engine) as db:
        owner.decide(
            db,
            command=DecideCompletionReview(
                context=context(job.reviewer_id, WRITE),
                access=job.access,
                enrollment_id=preview.enrollment_id,
                submission_id=submitted.submission_id,
                expected_generation=submitted.generation,
                policy_revision_id=preview.policy_revision_id,
                results=tuple(
                    CriterionResult(
                        item.id,
                        CriterionVerdict.passed if passed else CriterionVerdict.failed,
                        "Exact criterion verified"
                        if passed
                        else "Repair verification failed",
                    )
                    for item in preview.criteria
                ),
            ),
        )


def approve(engine: Engine, command: ReviewFailedScopeReplacement) -> UUID:
    with Session(engine) as db:
        return owner.review_failed_scope_replacement(db, command=command).decision_id


def revoke(
    engine: Engine, command: ReviewFailedScopeReplacement, decision_id: UUID
) -> None:
    with Session(engine) as db:
        owner.revoke_failed_scope_replacement(
            db,
            command=RevokeFailedScopeReplacement(
                context=context(command.access.principal_id, WRITE),
                access=command.access,
                source_enrollment_id=command.source_enrollment_id,
                decision_id=decision_id,
                expected_generation=command.expected_generation + 1,
            ),
        )


def test_failed_a_reviewed_b_accepted_b_customer_closes(native_engine: Engine) -> None:
    jobs = pair(native_engine)
    decision_id = approve(native_engine, jobs.command)
    with pytest.raises(DomainError, match="completion pending"):
        request(native_engine, jobs.source)
    accept_enrolled(native_engine, jobs.replacement)
    token_id = request(native_engine, jobs.source)
    with Session(native_engine) as db:
        bases = db.scalars(select(SupportResolutionBasis)).all()
        assert len(bases) == 2
        endpoint = db.scalar(select(SupportResolutionEffectiveBasis))
        lineage = db.scalar(select(SupportResolutionReplacementLineage))
        assert endpoint and endpoint.work_order_id == jobs.source.work_order_id
        assert endpoint.effective_work_order_id == jobs.replacement.work_order_id
        assert lineage and lineage.position == 0 and lineage.decision_id == decision_id
        assert db.get(WorkOrder, jobs.source.work_order_id).status == "canceled"
    with Session(native_engine) as db:
        token = db.get(TicketAccessToken, token_id)
        assert token is not None
        support.Tickets.confirm_resolution(db, token)
    with Session(native_engine) as db:
        assert db.get(Ticket, jobs.source.ticket_id).status == "closed"


@pytest.mark.parametrize(
    "state", ["scheduled", "canceled", "rejected", "revoked_acceptance"]
)
def test_effective_endpoint_must_be_currently_accepted(
    native_engine: Engine, state: str
) -> None:
    jobs = pair(native_engine)
    approve(native_engine, jobs.command)
    if state in {"scheduled", "canceled"}:
        with Session(native_engine) as db:
            row = db.get(WorkOrder, jobs.replacement.work_order_id)
            assert row is not None
            row.status = state
            db.commit()
    else:
        accept_enrolled(native_engine, jobs.replacement, passed=state != "rejected")
        if state == "revoked_acceptance":
            from app.services.field.completion_review_contracts import (
                RevokeCompletionReview,
            )

            with Session(native_engine) as db:
                head = db.get(
                    CompletionReviewHead, jobs.command.replacement_enrollment_id
                )
                assert head and head.decision_id
                revoke_command = RevokeCompletionReview(
                    context=context(jobs.replacement.reviewer_id, WRITE),
                    access=jobs.replacement.access,
                    enrollment_id=jobs.command.replacement_enrollment_id,
                    decision_id=head.decision_id,
                    expected_generation=head.generation,
                )
            with Session(native_engine) as db:
                owner.revoke(db, command=revoke_command)
    with pytest.raises(DomainError, match="completion pending"):
        request(native_engine, jobs.source)


@pytest.mark.parametrize(
    "drift",
    [
        "revoke",
        "source_policy",
        "source_criteria",
        "source_generation",
        "replacement_generation",
    ],
)
def test_confirmation_refuses_replacement_lineage_drift(
    native_engine: Engine, drift: str
) -> None:
    jobs = pair(native_engine)
    decision_id = approve(native_engine, jobs.command)
    accept_enrolled(native_engine, jobs.replacement)
    token_id = request(native_engine, jobs.source)
    if drift == "revoke":
        revoke(native_engine, jobs.command, decision_id)
    elif drift == "source_policy":
        with Session(native_engine) as db:
            owner.publish_policy(
                db,
                command=PublishCompletionReviewPolicy(
                    context=context(jobs.source.reviewer_id, WRITE),
                    access=jobs.source.access,
                    scope=jobs.source.enrollment.policy_scope,
                    expected_generation=1,
                    independent_reviewer_required=False,
                ),
            )
    elif drift == "source_criteria":
        with Session(native_engine) as db:
            owner.publish_criteria(
                db,
                command=PublishCompletionCriteria(
                    context=context(jobs.source.reviewer_id, WRITE),
                    access=jobs.source.access,
                    enrollment_id=jobs.command.source_enrollment_id,
                    expected_generation=1,
                    criteria=(
                        replace(
                            jobs.source.enrollment.criteria[0],
                            statement="Changed failed scope",
                        ),
                    ),
                ),
            )
    else:
        # Force a fresh canonical assignment through the dispatch owner.
        from app.services.work_order_assignment_contracts import (
            TechnicianAssignmentTarget,
            WorkOrderAssignmentCommand,
        )
        from app.services.work_order_commands import work_order_commands

        job = jobs.source if drift == "source_generation" else jobs.replacement
        with Session(native_engine) as db:
            order = db.get(WorkOrder, job.work_order_id)
            assert order is not None
            order.status = "scheduled"
            db.commit()
            expected_revision = order.updated_at
            profile = db.scalar(
                select(FieldJobEvent.author_technician_id).where(
                    FieldJobEvent.id == job.completed_event_id
                )
            )
        assert profile is not None
        with Session(native_engine) as db:
            work_order_commands.assign(
                db,
                command=WorkOrderAssignmentCommand(
                    access=job.access,
                    work_order_public_id=job.public_id,
                    target=TechnicianAssignmentTarget(profile),
                    expected_revision=expected_revision,
                    expected_assignment_generation=job.assignment_generation,
                    reason="Fresh assignment invalidates frozen lineage",
                ),
                context=context(job.reviewer_id, "operations:dispatch:assign"),
            )
    with Session(native_engine) as db:
        token = db.get(TicketAccessToken, token_id)
        assert token is not None
        with pytest.raises(DomainError):
            support.Tickets.confirm_resolution(db, token)
    with Session(native_engine) as db:
        assert db.get(Ticket, jobs.source.ticket_id).status == "pending_confirmation"
        token = db.get(TicketAccessToken, token_id)
        assert token and token.is_active and token.responded_at is None


def test_ordered_a_b_c_chain_retains_every_required_scope(
    native_engine: Engine,
) -> None:
    jobs = pair(native_engine)
    first = approve(native_engine, jobs.command)
    third = prepare(native_engine, ticket_id=jobs.source.ticket_id)
    enroll(native_engine, third)
    fail(native_engine, jobs.replacement)
    second_command = command_for(native_engine, jobs.replacement, third)
    second = approve(native_engine, second_command)
    accept_enrolled(native_engine, third)
    token_id = request(native_engine, jobs.source)
    with Session(native_engine) as db:
        rows = db.scalars(
            select(SupportResolutionReplacementLineage)
            .where(
                SupportResolutionReplacementLineage.work_order_id
                == jobs.source.work_order_id
            )
            .order_by(SupportResolutionReplacementLineage.position)
        ).all()
        assert tuple(row.decision_id for row in rows) == (first, second)
        assert db.scalar(select(func.count()).select_from(SupportResolutionBasis)) == 3
        token = db.get(TicketAccessToken, token_id)
        assert token is not None
        support.Tickets.confirm_resolution(db, token)


def test_rejected_submission_is_exact_failure_basis(native_engine: Engine) -> None:
    source = prepare(native_engine)
    replacement = prepare(native_engine, ticket_id=source.ticket_id)
    enroll(native_engine, source)
    enroll(native_engine, replacement)
    accept_enrolled(native_engine, source, passed=False)
    command = command_for(native_engine, source, replacement)
    assert isinstance(command.failure_basis, RejectedSubmissionFailure)
    bad = replace(
        command, failure_basis=replace(command.failure_basis, decision_id=uuid4())
    )
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="failure basis stale"):
            owner.review_failed_scope_replacement(db, command=bad)
    approve(native_engine, command)
    accept_enrolled(native_engine, replacement)
    request(native_engine, source)


@pytest.mark.parametrize("same_intent", [True, False])
def test_concurrent_review_and_exact_replay(
    native_engine: Engine, same_intent: bool
) -> None:
    jobs = pair(native_engine)
    barrier = Barrier(2)

    def run(index: int) -> str:
        command = (
            jobs.command
            if same_intent or index == 0
            else replace(jobs.command, context=context(jobs.source.reviewer_id, WRITE))
        )
        barrier.wait(timeout=10)
        try:
            with Session(native_engine) as db:
                outcome = owner.review_failed_scope_replacement(db, command=command)
                return "replay" if outcome.replayed else "approved"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(run, (0, 1)))
    assert results.count("approved") == 1
    assert (
        ("replay" in results)
        if same_intent
        else ("operations.field_completion.stale_state" in results)
    )
    with Session(native_engine) as db:
        assert (
            db.scalar(select(func.count()).select_from(CompletionReplacementDecision))
            == 1
        )
        assert (
            db.scalar(select(func.count()).select_from(CompletionReplacementReceipt))
            == 1
        )


def test_no_cycle_fanin_branch_or_uncovered_scope(native_engine: Engine) -> None:
    jobs = pair(native_engine)
    third = prepare(native_engine, ticket_id=jobs.source.ticket_id)
    fail(native_engine, third)
    enroll(native_engine, third)
    other = command_for(native_engine, third, jobs.replacement)
    uncovered = replace(jobs.command, coverage=())
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="criterion mismatch"):
            owner.review_failed_scope_replacement(db, command=uncovered)
    approve(native_engine, jobs.command)
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="replacement conflict"):
            owner.review_failed_scope_replacement(db, command=other)
    fail(native_engine, jobs.replacement)
    cycle = replace(
        jobs.command,
        context=context(jobs.replacement.reviewer_id, WRITE),
        access=jobs.replacement.access,
        source_enrollment_id=jobs.command.replacement_enrollment_id,
        replacement_enrollment_id=jobs.command.source_enrollment_id,
        source_criteria_revision_id=jobs.command.replacement_criteria_revision_id,
        replacement_criteria_revision_id=jobs.command.source_criteria_revision_id,
        source_policy_revision_id=jobs.command.replacement_policy_revision_id,
        replacement_policy_revision_id=jobs.command.source_policy_revision_id,
        source_assignment_generation=jobs.command.replacement_assignment_generation,
        replacement_assignment_generation=jobs.command.source_assignment_generation,
        expected_work_order_public_id=jobs.replacement.public_id,
    )
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="replacement cycle"):
            owner.review_failed_scope_replacement(db, command=cycle)
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="replacement conflict"):
            owner.review_failed_scope_replacement(
                db,
                command=replace(
                    jobs.command,
                    context=context(jobs.source.reviewer_id, WRITE),
                    expected_generation=1,
                ),
            )


def test_event_failure_rolls_back_decision_head_receipt(
    native_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = pair(native_engine)

    def failure(
        db: Session, event_type: EventType, payload: dict[str, object], *, actor: str
    ) -> None:
        raise RuntimeError("Synthetic outbox failure")

    monkeypatch.setattr(owner, "emit_event", failure)
    with Session(native_engine) as db:
        with pytest.raises(RuntimeError, match="Synthetic outbox failure"):
            owner.review_failed_scope_replacement(db, command=jobs.command)
    with Session(native_engine) as db:
        assert (
            db.scalar(select(func.count()).select_from(CompletionReplacementDecision))
            == 0
        )
        assert (
            db.scalar(select(func.count()).select_from(CompletionReplacementReceipt))
            == 0
        )
        assert db.scalar(text("SELECT count(*) FROM completion_replacement_heads")) == 0


def test_immutable_decision_and_request_lineage_reject_sql_mutation(
    native_engine: Engine,
) -> None:
    jobs = pair(native_engine)
    decision_id = approve(native_engine, jobs.command)
    accept_enrolled(native_engine, jobs.replacement)
    request(native_engine, jobs.source)
    for statement in (
        "UPDATE completion_replacement_decisions SET reason='changed' WHERE id=:id",
        "DELETE FROM completion_replacement_coverage WHERE decision_id=:id",
        "DELETE FROM support_resolution_replacement_lineage WHERE decision_id=:id",
    ):
        with Session(native_engine) as db:
            with pytest.raises(DBAPIError):
                db.execute(text(statement), {"id": decision_id})
                db.commit()


@pytest.mark.parametrize("winner", ["revoke", "grace"])
def test_revocation_and_request_bound_grace_share_one_fence(
    native_engine: Engine, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    from app.services.runtime_durable_timers import fire_due_timers

    jobs = pair(native_engine)
    decision_id = approve(native_engine, jobs.command)
    accept_enrolled(native_engine, jobs.replacement)
    request(native_engine, jobs.source)
    future = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(support, "_now", lambda: future)
    original = owner.lock_work_order_linkage
    held, loser_started = Event(), Event()
    race_thread = local()

    def fence(db: Session, *, query: WorkOrderLinkageFence) -> None:
        original(db, query=query)
        if getattr(race_thread, "winner", False):
            held.set()
            assert loser_started.wait(timeout=10)

    import app.services.topology.outage_completion as outage_owner
    import app.services.work_order_commands as work_owner

    monkeypatch.setattr(owner, "lock_work_order_linkage", fence)
    monkeypatch.setattr(work_owner, "lock_work_order_linkage", fence)
    # Support grace prepares the Incident/Ticket graph through this imported
    # alias. Coordinate after its real native fence, just like revocation.
    monkeypatch.setattr(outage_owner, "lock_work_order_linkage", fence)

    def run(action: str) -> str:
        if action != winner:
            loser_started.set()
        try:
            with Session(native_engine) as db:
                # Timer delivery opens a fresh owner Session on this thread.
                # Keep coordination outside Session.info so both real owners hit it.
                race_thread.winner = action == winner
                if action == "revoke":
                    owner.revoke_failed_scope_replacement(
                        db,
                        command=RevokeFailedScopeReplacement(
                            context=context(jobs.source.reviewer_id, WRITE),
                            access=jobs.source.access,
                            source_enrollment_id=jobs.command.source_enrollment_id,
                            decision_id=decision_id,
                            expected_generation=1,
                        ),
                    )
                else:
                    fire_due_timers(
                        db,
                        now=future,
                        context=CommandContext.system(
                            actor="timer",
                            scope="timer",
                            reason="Reviewed recovery grace due",
                        ),
                    )
                return action
        except DomainError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, winner)
        if not held.wait(timeout=10):
            # Surface pre-fence worker errors instead of hiding them as a timeout.
            first.result(timeout=1)
            pytest.fail("Winner did not acquire the shared linkage fence")
        second = pool.submit(run, "grace" if winner == "revoke" else "revoke")
        assert first.result(timeout=20) == winner
        second.result(timeout=20)
    with Session(native_engine) as db:
        assert db.get(Ticket, jobs.source.ticket_id).status == (
            "pending_confirmation" if winner == "revoke" else "closed"
        )
