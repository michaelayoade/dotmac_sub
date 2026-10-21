"""Authored migrated-PG support closure acceptance; coordinator executes it."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_completion_review import (
    CompletionEnrollment,
    CompletionReviewHead,
)
from app.models.field_job_event import FieldJobEvent
from app.models.rbac import Permission, SystemUserPermission
from app.models.subscriber import Reseller, Subscriber
from app.models.support import SupportResolutionBasis, SupportResolutionRequest, Ticket
from app.models.work_order import WorkOrder
from app.schemas.support import TicketUpdate
from app.services import support
from app.services.domain_errors import DomainError
from app.services.field import completion_review as completion
from app.services.field.completion_review_contracts import (
    READ,
    WRITE,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionProvenance,
    CriterionObservation,
    CriterionResult,
    CriterionVerdict,
    DecideCompletionReview,
    EnrollCompletionReview,
    FieldSubmissionPreviewQuery,
    IssuedCompletionCriterion,
    PublishCompletionReviewPolicy,
    RevokeCompletionReview,
    SubmitCompletionEvidence,
)
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.support_resolution_contracts import SupportResolutionRequestCommand
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    WorkOrderAssignmentCommand,
    WorkOrderLinkageFence,
)
from app.services.work_order_commands import work_order_commands
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as engineering_native_engine,
)
from tests.staff_identity_fixtures import add_bound_staff_user

native_engine = engineering_native_engine


def context(actor: UUID, scope: str) -> CommandContext:
    return CommandContext.system(
        actor=str(actor),
        scope=scope,
        reason="Synthetic support completion acceptance",
        idempotency_key=str(uuid4()),
    )


@dataclass(frozen=True, kw_only=True)
class NativeJob:
    ticket_id: UUID
    work_order_id: UUID
    public_id: str
    reviewer_id: UUID
    technician_id: UUID
    access: PermissionAdmission
    enrollment: EnrollCompletionReview
    assignment_generation: UUID
    completed_event_id: UUID


def prepare(engine: Engine, *, ticket_id: UUID | None = None) -> NativeJob:
    with Session(engine) as db:
        reviewer, _ = add_bound_staff_user(db)
        technician, _ = add_bound_staff_user(db)
        profile = TechnicianProfile(
            person_id=technician.id, system_user_id=technician.id, is_active=True
        )
        db.add(profile)
        required = {READ, WRITE, "operations:dispatch:assign"}
        permissions = db.scalars(
            select(Permission).where(
                Permission.key.in_(required), Permission.is_active.is_(True)
            )
        ).all()
        assert {permission.key for permission in permissions} == required
        for permission in permissions:
            db.add(
                SystemUserPermission(
                    system_user_id=reviewer.id, permission_id=permission.id
                )
            )
        if ticket_id is None:
            reseller = Reseller(
                name="Synthetic support completion reseller",
                code=f"support-{uuid4().hex}",
                is_active=True,
            )
            db.add(reseller)
            db.flush()
            subscriber = Subscriber(
                reseller_id=reseller.id,
                first_name="Synthetic",
                last_name="Completion",
                email=f"support-{uuid4()}@example.invalid",
            )
            db.add(subscriber)
            db.flush()
            ticket = Ticket(
                title="Synthetic enrolled repair",
                subscriber_id=subscriber.id,
                status="open",
            )
            db.add(ticket)
            db.flush()
        else:
            ticket = db.get(Ticket, ticket_id)
            assert ticket is not None
        work = WorkOrder(
            title="Synthetic customer repair",
            subscriber_id=ticket.subscriber_id,
            origin_ticket_id=ticket.id,
            status="draft",
            work_order_kind="customer",
        )
        db.add(work)
        db.flush()
        work_id, public_id, revision = work.id, work.public_id, work.updated_at
        profile_id, reviewer_id, technician_id, ticket_id = (
            profile.id,
            reviewer.id,
            technician.id,
            ticket.id,
        )
        db.commit()
    access = PermissionAdmission(reviewer_id, "system_user", frozenset(required))
    with Session(engine) as db:
        assigned = work_order_commands.assign(
            db,
            command=WorkOrderAssignmentCommand(
                access=access,
                work_order_public_id=public_id,
                target=TechnicianAssignmentTarget(profile_id),
                expected_revision=revision,
                reason="Assign synthetic visit",
            ),
            context=context(reviewer_id, "operations:dispatch:assign"),
        )
    assert assigned.assignment_generation is not None
    with Session(engine) as db:
        row = db.get(WorkOrder, work_id)
        assert row is not None
        row.status = "completed"
        now = datetime.now(UTC)
        event = FieldJobEvent(
            work_order_mirror_id=work_id,
            author_technician_id=profile_id,
            person_id=technician_id,
            system_user_id=technician_id,
            event="complete",
            previous_status="in_progress",
            new_status="completed",
            occurred_at=now,
            received_at=now,
            assignment_generation=assigned.assignment_generation,
            client_event_id=uuid4(),
        )
        db.add(event)
        db.flush()
        event_id = event.id
        db.commit()
    scope = CompletionPolicyScope(
        kind=CompletionPolicyScopeKind.work_order, work_order_public_id=public_id
    )
    with Session(engine) as db:
        completion.publish_policy(
            db,
            command=PublishCompletionReviewPolicy(
                context=context(reviewer_id, WRITE),
                access=access,
                scope=scope,
                expected_generation=0,
                independent_reviewer_required=True,
            ),
        )
    return NativeJob(
        ticket_id=ticket_id,
        work_order_id=work_id,
        public_id=public_id,
        reviewer_id=reviewer_id,
        technician_id=technician_id,
        access=access,
        assignment_generation=assigned.assignment_generation,
        completed_event_id=event_id,
        enrollment=EnrollCompletionReview(
            context=context(reviewer_id, WRITE),
            access=access,
            work_order_public_id=public_id,
            provenance=CompletionProvenance.native_work_order,
            policy_scope=scope,
            criteria=(
                IssuedCompletionCriterion(
                    id=uuid4(),
                    order=0,
                    statement="Repair verified",
                    verification_method="Measure service",
                ),
            ),
        ),
    )


def accept(engine: Engine, job: NativeJob) -> UUID:
    with Session(engine) as db:
        enrolled = completion.enroll(db, command=job.enrollment)
    with Session(engine) as db:
        preview = completion.field_submission_preview(
            db,
            query=FieldSubmissionPreviewQuery(
                requester_system_user_id=job.technician_id,
                work_order_public_id=job.public_id,
            ),
        )
    assert enrolled.enrollment_id == preview.enrollment_id
    assert preview.enrollment_id is not None
    assert preview.criteria_revision_id is not None
    assert preview.policy_revision_id is not None
    with Session(engine) as db:
        submitted = completion.submit_evidence(
            db,
            command=SubmitCompletionEvidence(
                context=context(job.technician_id, "field:completion:submit"),
                requester_system_user_id=job.technician_id,
                enrollment_id=preview.enrollment_id,
                criteria_revision_id=preview.criteria_revision_id,
                completed_event_id=job.completed_event_id,
                assignment_generation=job.assignment_generation,
                expected_generation=0,
                policy_revision_id=preview.policy_revision_id,
                observations=(
                    CriterionObservation(
                        job.enrollment.criteria[0].id, "Measured repaired service"
                    ),
                ),
                expected_work_order_public_id=job.public_id,
            ),
        )
    assert submitted.submission_id is not None
    with Session(engine) as db:
        decided = completion.decide(
            db,
            command=DecideCompletionReview(
                context=context(job.reviewer_id, WRITE),
                access=job.access,
                enrollment_id=preview.enrollment_id,
                submission_id=submitted.submission_id,
                expected_generation=submitted.generation,
                policy_revision_id=preview.policy_revision_id,
                results=(
                    CriterionResult(
                        job.enrollment.criteria[0].id,
                        CriterionVerdict.passed,
                        "Verified measurement",
                    ),
                ),
            ),
        )
    assert decided.decision_id is not None
    return enrolled.enrollment_id


def request(engine: Engine, job: NativeJob) -> UUID:
    with Session(engine) as db:
        _, token = support.Tickets.request_resolution_confirmation(
            db,
            str(job.ticket_id),
            command=SupportResolutionRequestCommand(
                context=context(
                    job.reviewer_id, "support.ticket:request_resolution_confirmation"
                ),
                ticket_id=job.ticket_id,
                actor_id=job.reviewer_id,
            ),
        )
        return token.id


@pytest.mark.parametrize("winner", ["close", "enroll"])
def test_close_and_enrollment_share_one_fence(
    native_engine: Engine, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    import app.services.work_order_commands as work_owner

    job = prepare(native_engine)
    held, loser_started = Event(), Event()
    original = work_owner.lock_work_order_linkage

    def fence(db: Session, *, query: WorkOrderLinkageFence) -> None:
        original(db, query=query)
        if db.info.get("gate_race_winner"):
            held.set()
            assert loser_started.wait(timeout=10)

    monkeypatch.setattr(work_owner, "lock_work_order_linkage", fence)
    monkeypatch.setattr(completion, "lock_work_order_linkage", fence)

    def run(action: str) -> str:
        if action != winner:
            loser_started.set()
        try:
            with Session(native_engine) as db:
                db.info["gate_race_winner"] = action == winner
                if action == "close":
                    support.Tickets.update(
                        db, str(job.ticket_id), TicketUpdate(status="closed")
                    )
                    return "closed"
                completion.enroll(db, command=job.enrollment)
                return "enrolled"
        except DomainError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, winner)
        assert held.wait(timeout=10)
        other = "enroll" if winner == "close" else "close"
        second = pool.submit(run, other)
        assert first.result(timeout=20) == (
            "closed" if winner == "close" else "enrolled"
        )
        assert second.result(timeout=20) == "refused"
    with Session(native_engine) as db:
        ticket = db.get(Ticket, job.ticket_id)
        assert ticket is not None
        assert (ticket.status == "closed") == (winner == "close")
        assert db.scalar(
            select(func.count())
            .select_from(CompletionEnrollment)
            .where(CompletionEnrollment.work_order_id == job.work_order_id)
        ) == (1 if winner == "enroll" else 0)


def test_later_enrolled_job_invalidates_pinned_customer_request(
    native_engine: Engine,
) -> None:
    first = prepare(native_engine)
    accept(native_engine, first)
    token_id = request(native_engine, first)
    second = prepare(native_engine, ticket_id=first.ticket_id)
    with Session(native_engine) as db:
        completion.enroll(db, command=second.enrollment)
    from app.models.support import TicketAccessToken

    with Session(native_engine) as db, pytest.raises(DomainError):
        token = db.get(TicketAccessToken, token_id)
        assert token is not None
        support.Tickets.confirm_resolution(db, token)
    with Session(native_engine) as db:
        ticket = db.get(Ticket, first.ticket_id)
        token = db.get(TicketAccessToken, token_id)
        assert ticket is not None and ticket.status == "pending_confirmation"
        assert token is not None and token.is_active


def test_revoked_acceptance_blocks_customer_confirmation(native_engine: Engine) -> None:
    job = prepare(native_engine)
    enrollment_id = accept(native_engine, job)
    token_id = request(native_engine, job)
    with Session(native_engine) as db:
        head = db.get(CompletionReviewHead, enrollment_id)
        assert head is not None and head.decision_id is not None
        decision_id, generation = head.decision_id, head.generation
    with Session(native_engine) as db:
        completion.revoke(
            db,
            command=RevokeCompletionReview(
                context=context(job.reviewer_id, WRITE),
                access=job.access,
                enrollment_id=enrollment_id,
                decision_id=decision_id,
                expected_generation=generation,
            ),
        )
    from app.models.support import TicketAccessToken

    with Session(native_engine) as db, pytest.raises(DomainError):
        token = db.get(TicketAccessToken, token_id)
        assert token is not None
        support.Tickets.confirm_resolution(db, token)
    with Session(native_engine) as db:
        assert (
            db.scalar(select(Ticket.status).where(Ticket.id == job.ticket_id))
            == "pending_confirmation"
        )


def test_native_request_and_basis_reject_late_mutation(native_engine: Engine) -> None:
    job = prepare(native_engine)
    accept(native_engine, job)
    token_id = request(native_engine, job)
    with Session(native_engine) as db:
        native = db.scalar(
            select(SupportResolutionRequest).where(
                SupportResolutionRequest.token_id == token_id
            )
        )
        assert native is not None
        request_id = native.id
        assert (
            db.scalar(
                select(func.count())
                .select_from(SupportResolutionBasis)
                .where(SupportResolutionBasis.request_id == request_id)
            )
            == 1
        )
    with Session(native_engine) as db, pytest.raises(DBAPIError):
        db.execute(
            text("UPDATE support_resolution_requests SET grace_hours=48 WHERE id=:id"),
            {"id": request_id},
        )
        db.commit()
    with Session(native_engine) as db, pytest.raises(DBAPIError):
        db.execute(
            text("DELETE FROM support_resolution_bases WHERE request_id=:id"),
            {"id": request_id},
        )
        db.commit()


def test_unchanged_native_basis_allows_customer_confirmation(
    native_engine: Engine,
) -> None:
    from app.models.support import SupportResolutionAttempt, TicketAccessToken

    job = prepare(native_engine)
    accept(native_engine, job)
    token_id = request(native_engine, job)
    with Session(native_engine) as db:
        token = db.get(TicketAccessToken, token_id)
        assert token is not None
        assert support.Tickets.confirm_resolution(db, token).status == "closed"
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SupportResolutionAttempt)
                .where(
                    SupportResolutionAttempt.ticket_id == job.ticket_id,
                    SupportResolutionAttempt.outcome == "confirmed",
                    SupportResolutionAttempt.source == "customer",
                )
            )
            == 1
        )


def test_dispute_admitted_before_replacement_refuses_after_lock_wait(
    native_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.support import TicketAccessToken

    job = prepare(native_engine)
    accept(native_engine, job)
    old_id = request(native_engine, job)
    admitted, replaced = Event(), Event()
    original = support._lock_resolution_ticket

    def lock_ticket(db: Session, ticket_id: UUID) -> Ticket | None:
        if db.info.get("stale_dispute"):
            admitted.set()
            assert replaced.wait(timeout=10)
        return original(db, ticket_id)

    monkeypatch.setattr(support, "_lock_resolution_ticket", lock_ticket)

    def dispute() -> str:
        with Session(native_engine) as db:
            token = db.get(TicketAccessToken, old_id)
            assert token is not None and token.is_active
            db.info["stale_dispute"] = True
            try:
                support.Tickets.dispute_resolution(
                    db, token, reason="Old admitted response"
                )
            except DomainError as error:
                return error.code
            return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(dispute)
        assert admitted.wait(timeout=10)
        current_id = request(native_engine, job)
        replaced.set()
        assert pending.result(timeout=20) == "resolution_confirmation_inactive"
    with Session(native_engine) as db:
        ticket = db.get(Ticket, job.ticket_id)
        current = db.get(TicketAccessToken, current_id)
        assert ticket is not None and ticket.status == "pending_confirmation"
        assert (
            current is not None and current.is_active and current.responded_at is None
        )


def test_request_bound_grace_timer_closes_accepted_enrolled_basis(
    native_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from app.models.support import SupportResolutionAttempt, TicketAccessToken
    from app.services.runtime_durable_timers import fire_due_timers

    job = prepare(native_engine)
    accept(native_engine, job)
    token_id = request(native_engine, job)
    future = datetime.now(UTC) + timedelta(days=2)

    def now() -> datetime:
        return future

    monkeypatch.setattr(support, "_now", now)
    with Session(native_engine) as db:
        fired = fire_due_timers(
            db,
            now=future,
            context=CommandContext.system(
                actor="timer", scope="timer", reason="Enrolled grace due"
            ),
        )
    assert fired
    with Session(native_engine) as db:
        token = db.get(TicketAccessToken, token_id)
        assert token is not None and not token.is_active
        assert (
            db.scalar(select(Ticket.status).where(Ticket.id == job.ticket_id))
            == "closed"
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(SupportResolutionAttempt)
                .where(
                    SupportResolutionAttempt.ticket_id == job.ticket_id,
                    SupportResolutionAttempt.outcome == "confirmed",
                    SupportResolutionAttempt.source == "grace",
                )
            )
            == 1
        )
