"""680 migrated PostgreSQL acceptance; coordinator test-server execution only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.field_completion_review import (
    CompletionEnrollment,
    CompletionReviewDecision,
)
from app.models.field_job_event import FieldJobEvent
from app.models.field_visit_followup import (
    FieldVisitFollowup,
    FieldVisitFollowupRevision,
)
from app.models.service_team import ServiceTeam
from app.services.domain_errors import DomainError
from app.services.field import visit_followup as owner
from app.services.field.visit_followup_contracts import (
    WRITE,
    DecideVisitFollowup,
    FailureSource,
    FailureSourceKind,
    FollowupDisposition,
    FollowupResponsibility,
    OpenVisitFollowup,
)
from app.services.owner_commands import CommandContext
from tests.integration.test_failed_scope_replacement_acceptance import (
    accept_enrolled,
    enroll,
    fail,
)
from tests.integration.test_support_completion_gate_acceptance import (
    native_engine as support_native_engine,
)
from tests.integration.test_support_completion_gate_acceptance import prepare

native_engine = support_native_engine


def command_for(engine, *, rejected=False):
    job = prepare(engine)
    if rejected:
        enroll(engine, job)
        accept_enrolled(engine, job, passed=False)
    else:
        fail(engine, job)
    with Session(engine) as db:
        team = ServiceTeam(name=f"Synthetic680 {uuid4()}", is_active=True)
        db.add(team)
        db.flush()
        team_id = team.id
        if rejected:
            source_id = db.scalar(
                select(CompletionReviewDecision.id)
                .join(
                    CompletionEnrollment,
                    CompletionEnrollment.id == CompletionReviewDecision.enrollment_id,
                )
                .where(
                    CompletionEnrollment.work_order_id == job.work_order_id,
                    CompletionReviewDecision.state == "rejected",
                )
            )
        else:
            source_id = db.scalar(
                select(FieldJobEvent.id).where(
                    FieldJobEvent.work_order_mirror_id == job.work_order_id,
                    FieldJobEvent.event == "unable_to_complete",
                )
            )
        assert source_id is not None
        db.commit()
    return OpenVisitFollowup(
        CommandContext.system(
            actor=str(job.reviewer_id),
            scope=WRITE,
            reason="Synthetic680 explicit responsible followup",
            idempotency_key=str(uuid4()),
        ),
        job.access,
        FailureSource(
            FailureSourceKind.rejected_decision
            if rejected
            else FailureSourceKind.unable_event,
            source_id,
        ),
        FollowupResponsibility(team_id, None, datetime.now(UTC) + timedelta(days=1)),
        "Synthetic680 explicit responsible followup",
        job.access if rejected else None,
    )


@pytest.mark.parametrize("same_intent", [True, False])
def test_concurrent_exact_source_and_replay(native_engine, same_intent):
    command = command_for(native_engine)
    barrier = Barrier(2)

    def run(index):
        candidate = (
            command
            if same_intent or index == 0
            else replace(
                command, context=replace(command.context, idempotency_key=str(uuid4()))
            )
        )
        barrier.wait(timeout=10)
        try:
            with Session(native_engine) as db:
                outcome = owner.open_followup(db, command=candidate)
            return "replay" if outcome.replayed else "created"
        except DomainError as exc:
            return exc.code.rsplit(".", 1)[-1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    assert sorted(results) == sorted(
        ["created", "replay" if same_intent else "already_accounted"]
    )
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(FieldVisitFollowup)
                .where(FieldVisitFollowup.field_event_id == command.source.source_id)
            )
            == 1
        )


def test_rejected_source_needs_fresh_completion_read(native_engine):
    command = command_for(native_engine, rejected=True)
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="completion history admission"):
            owner.open_followup(
                db, command=replace(command, completion_read_access=None)
            )
    with Session(native_engine) as db:
        outcome = owner.open_followup(db, command=command)
    assert outcome.source == command.source


def test_waiting_responsibility_deactivation_refuses_atomically(
    native_engine, monkeypatch
):
    command = command_for(native_engine)
    started = Event()
    waiter = {}
    original = owner._responsibility

    def responsibility(db, value, failure):
        waiter["pid"] = db.scalar(text("SELECT pg_backend_pid()"))
        started.set()
        return original(db, value, failure)

    monkeypatch.setattr(owner, "_responsibility", responsibility)

    def run():
        with Session(native_engine) as db:
            try:
                owner.open_followup(db, command=command)
                return "unexpected"
            except DomainError as exc:
                return exc.code

    with Session(native_engine) as holder:
        team = holder.scalar(
            select(ServiceTeam)
            .where(ServiceTeam.id == command.responsibility.team_id)
            .with_for_update()
        )
        team.is_active = False
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            assert started.wait(5)
            deadline = monotonic() + 10
            with native_engine.connect() as observer:
                while monotonic() < deadline:
                    if observer.scalar(
                        text(
                            "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": waiter["pid"]},
                    ):
                        break
                    observer.rollback()
                    sleep(0.01)
                else:
                    pytest.fail("owner did not wait on the responsible team lock")
            # Commit releases the team lock; the owner must observe the new value.
            holder.commit()
            assert future.result(timeout=15).endswith("responsibility_unavailable")
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(FieldVisitFollowup)
                .where(FieldVisitFollowup.field_event_id == command.source.source_id)
            )
            == 0
        )


def test_append_only_provenance_and_stale_revision(native_engine):
    command = command_for(native_engine)
    with Session(native_engine) as db:
        first = owner.open_followup(db, command=command)
    next_command = DecideVisitFollowup(
        replace(command.context, idempotency_key=str(uuid4())),
        command.access,
        first.obligation_id,
        first.revision_id,
        command.responsibility,
        FollowupDisposition.revisit_required,
        command.reason,
    )
    with Session(native_engine) as db:
        second = owner.decide_followup(db, command=next_command)
    assert second.sequence == 2 and second.replacement_decision_id is None
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="Reload current evidence"):
            owner.decide_followup(
                db,
                command=replace(
                    next_command,
                    context=replace(command.context, idempotency_key=str(uuid4())),
                ),
            )
    with Session(native_engine) as db:
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE field_visit_followup_revisions SET reason='rewritten' WHERE id=:id"
                ),
                {"id": first.revision_id},
            )
            db.commit()
    with Session(native_engine) as db:
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE field_visit_followups SET field_event_id=:source WHERE id=:id"
                ),
                {"source": uuid4(), "id": first.obligation_id},
            )
            db.commit()
        db.rollback()
        assert (
            db.scalar(
                select(func.count())
                .select_from(FieldVisitFollowupRevision)
                .where(FieldVisitFollowupRevision.obligation_id == first.obligation_id)
            )
            == 2
        )


def test_exact_current_approved_replacement_linkage(native_engine):
    from tests.integration.test_failed_scope_replacement_acceptance import approve, pair

    jobs = pair(native_engine)
    approved = approve(native_engine, jobs.command)
    with Session(native_engine) as db:
        team = ServiceTeam(name=f"Synthetic680 approved {uuid4()}", is_active=True)
        db.add(team)
        db.flush()
        team_id = team.id
        db.commit()
    assert jobs.command.failure_basis.event_id is not None
    command = OpenVisitFollowup(
        CommandContext.system(
            actor=str(jobs.source.reviewer_id),
            scope=WRITE,
            reason="Synthetic680 reviewed replacement",
            idempotency_key=str(uuid4()),
        ),
        jobs.source.access,
        FailureSource(
            FailureSourceKind.unable_event, jobs.command.failure_basis.event_id
        ),
        FollowupResponsibility(team_id, None, datetime.now(UTC) + timedelta(days=1)),
        "Synthetic680 reviewed replacement",
    )
    with Session(native_engine) as db:
        first = owner.open_followup(db, command=command)
    linked = DecideVisitFollowup(
        replace(command.context, idempotency_key=str(uuid4())),
        command.access,
        first.obligation_id,
        first.revision_id,
        command.responsibility,
        FollowupDisposition.revisit_required,
        command.reason,
        approved,
        None,
        jobs.source.access,
        expected_replacement_generation=1,
    )
    with Session(native_engine) as db:
        outcome = owner.decide_followup(db, command=linked)
    assert outcome.replacement_decision_id == approved
    with Session(native_engine) as db:
        assert owner.decide_followup(db, command=linked).replayed
