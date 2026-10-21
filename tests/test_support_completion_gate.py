"""Non-authoritative fast support-gate tests with typed completion participants."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.support import (
    SupportResolutionAttempt,
    SupportResolutionRequest,
    Ticket,
    TicketAccessToken,
)
from app.schemas.support import TicketUpdate
from app.services import support
from app.services.domain_errors import DomainError
from app.services.field.completion_review_contracts import (
    TicketCompletionBasis,
    TicketCompletionBasisEntry,
)
from app.services.owner_commands import CommandContext
from app.services.support_resolution_contracts import SupportResolutionRequestCommand


@dataclass(kw_only=True)
class GateRig:
    db: Session
    ticket: Ticket
    basis: TicketCompletionBasis
    enrolled: bool = False
    refusal: str | None = None
    locks: list[UUID] = field(default_factory=list)


@pytest.fixture
def rig(
    db_session: Session, subscriber: Subscriber, monkeypatch: pytest.MonkeyPatch
) -> GateRig:
    ticket = Ticket(
        title="Completion gate test", subscriber_id=subscriber.id, status="open"
    )
    db_session.add(ticket)
    db_session.commit()
    state = GateRig(
        db=db_session,
        ticket=ticket,
        basis=TicketCompletionBasis(ticket_id=ticket.id, entries=()),
    )

    def fence(db: Session, ticket_id: UUID) -> None:
        state.locks.append(ticket_id)

    def presence(db: Session, ticket_id: UUID) -> bool:
        assert ticket_id == state.ticket.id
        return state.enrolled

    def basis(db: Session, ticket_id: UUID) -> TicketCompletionBasis:
        assert state.locks and state.locks[-1] == ticket_id
        if state.refusal:
            raise DomainError(
                code=state.refusal, message="Current completion acceptance is required"
            )
        return state.basis

    monkeypatch.setattr(support, "_lock_ticket_completion_linkage", fence)
    monkeypatch.setattr(support, "_linked_completion_enrollments", presence)
    monkeypatch.setattr(support, "_ticket_completion_basis", basis)
    return state


def request(rig: GateRig) -> TicketAccessToken:
    _, token = support.Tickets.request_resolution_confirmation(
        rig.db, str(rig.ticket.id), grace_hours=24
    )
    return token


def entry() -> TicketCompletionBasisEntry:
    return TicketCompletionBasisEntry(
        work_order_id=uuid4(),
        enrollment_id=uuid4(),
        criteria_revision_id=uuid4(),
        policy_revision_id=uuid4(),
        assignment_generation=uuid4(),
        completed_event_id=uuid4(),
        submission_id=uuid4(),
        accepted_decision_id=uuid4(),
        review_generation=1,
    )


@pytest.mark.parametrize("status", ["closed", "resolved"])
def test_direct_terminal_status_refuses_any_enrollment(
    rig: GateRig, status: str
) -> None:
    rig.enrolled = True
    with pytest.raises(support.SupportTicketError) as error:
        support.Tickets.update(rig.db, str(rig.ticket.id), TicketUpdate(status=status))
    assert error.value.code == "resolution_confirmation_required"
    rig.db.refresh(rig.ticket)
    assert rig.ticket.status == "open"
    assert (
        rig.db.scalar(select(func.count()).select_from(SupportResolutionRequest)) == 0
    )


def test_unenrolled_direct_close_keeps_legacy_behavior(rig: GateRig) -> None:
    result = support.Tickets.update(
        rig.db, str(rig.ticket.id), TicketUpdate(status="closed")
    )
    assert result.status == "closed"


def test_request_persists_immutable_native_request_and_exact_grace(
    rig: GateRig,
) -> None:
    token = request(rig)
    native = rig.db.scalar(
        select(SupportResolutionRequest).where(
            SupportResolutionRequest.token_id == token.id
        )
    )
    assert native is not None and native.ticket_id == rig.ticket.id
    assert native.grace_hours == 24
    assert native.command_id is not None
    deadline = support._resolution_deadline(rig.db, rig.ticket)
    rig.ticket.metadata_ = {"resolution_confirmation": {"grace_hours": 1}}
    rig.ticket.resolved_at = datetime.now(UTC) - timedelta(days=10)
    # New native requests retain their immutable deadline even without enrollment.
    assert support._resolution_deadline(rig.db, rig.ticket) == deadline
    native.grace_hours = 1
    with pytest.raises(ValueError, match="immutable"):
        rig.db.flush()
    rig.db.rollback()


def test_new_enrollment_after_request_requires_fresh_confirmation(rig: GateRig) -> None:
    token = request(rig)
    rig.basis = replace(rig.basis, entries=(entry(),))
    with pytest.raises(support.SupportTicketError) as error:
        support.Tickets.confirm_resolution(rig.db, token)
    assert error.value.code == "resolution_basis_changed"
    rig.db.refresh(rig.ticket)
    assert rig.ticket.status == "pending_confirmation"
    rig.db.refresh(token)
    assert token.is_active and token.responded_at is None


def test_missing_current_acceptance_blocks_request_without_token(rig: GateRig) -> None:
    rig.refusal = "operations.field_completion.not_accepted"
    with pytest.raises(DomainError):
        request(rig)
    assert rig.db.scalar(select(func.count()).select_from(TicketAccessToken)) == 0
    rig.db.refresh(rig.ticket)
    assert rig.ticket.status == "open"


def test_unchanged_request_can_confirm_and_records_attempt(rig: GateRig) -> None:
    token = request(rig)
    result = support.Tickets.confirm_resolution(rig.db, token)
    assert result.status == "closed"
    attempt = rig.db.scalar(select(SupportResolutionAttempt))
    assert (
        attempt is not None
        and attempt.outcome == "confirmed"
        and attempt.source == "customer"
    )


def test_grace_refusal_is_receipted_without_closing_or_consuming_capability(
    rig: GateRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = request(rig)
    future = datetime.now(UTC) + timedelta(days=2)

    def now() -> datetime:
        return future

    monkeypatch.setattr(support, "_now", now)
    rig.refusal = "operations.field_completion.revoked"
    from app.services.runtime_durable_timers import fire_due_timers

    rig.db.commit()
    fired = fire_due_timers(
        rig.db,
        now=future,
        context=CommandContext.system(
            actor="test-timer", scope="timer", reason="Grace due"
        ),
    )
    assert len(fired) == 1
    rig.db.refresh(rig.ticket)
    rig.db.refresh(token)
    assert rig.ticket.status == "pending_confirmation" and token.is_active
    attempt = rig.db.scalar(select(SupportResolutionAttempt))
    assert attempt is not None and attempt.outcome == "blocked_completion_basis"
    native = rig.db.scalar(select(SupportResolutionRequest))
    assert native is not None
    again = support.Tickets.consume_resolution_confirmation_due(
        rig.db,
        ticket_id=str(rig.ticket.id),
        event_id=fired[0].event_id,
        timer_id=fired[0].timer_id,
        timer_generation=fired[0].generation,
        resolution_request_id=native.id,
        context=CommandContext.system(
            actor="test-timer",
            scope="timer",
            reason="Retry",
            idempotency_key=str(fired[0].event_id),
        ),
    )
    assert again == "replayed"
    assert (
        rig.db.scalar(select(func.count()).select_from(SupportResolutionAttempt)) == 1
    )


def test_old_closed_ticket_is_not_reopened_after_new_enrollment(rig: GateRig) -> None:
    token = request(rig)
    support.Tickets.confirm_resolution(rig.db, token)
    rig.basis = replace(rig.basis, entries=(entry(),))
    rig.enrolled = True
    assert support.Tickets.confirm_resolution(rig.db, token).status == "closed"


def test_exact_request_retry_returns_active_receipt_without_extending_deadline(
    rig: GateRig,
) -> None:
    context = CommandContext.system(
        actor="support-test",
        scope="support.ticket:request_resolution_confirmation",
        reason="Customer request",
        idempotency_key=str(uuid4()),
    )
    command = SupportResolutionRequestCommand(
        context=context, ticket_id=rig.ticket.id, grace_hours=24
    )
    _, first = support.Tickets.request_resolution_confirmation(
        rig.db, str(rig.ticket.id), command=command
    )
    native = rig.db.scalar(select(SupportResolutionRequest))
    assert native is not None
    requested_at = native.requested_at
    _, retry = support.Tickets.request_resolution_confirmation(
        rig.db, str(rig.ticket.id), command=command
    )
    assert retry.id == first.id
    assert (
        rig.db.scalar(select(func.count()).select_from(SupportResolutionRequest)) == 1
    )
    assert rig.db.scalar(select(SupportResolutionRequest.requested_at)) == requested_at
    changed = replace(command, grace_hours=48)
    with pytest.raises(support.SupportTicketError) as error:
        support.Tickets.request_resolution_confirmation(
            rig.db, str(rig.ticket.id), command=changed
        )
    assert error.value.code == "resolution_idempotency_conflict"


def test_request_retry_cannot_resurrect_consumed_or_revoked_capability(
    rig: GateRig,
) -> None:
    command = SupportResolutionRequestCommand(
        context=CommandContext.system(
            actor="support-test",
            scope="support.ticket:request_resolution_confirmation",
            reason="Customer request",
            idempotency_key=str(uuid4()),
        ),
        ticket_id=rig.ticket.id,
    )
    _, token = support.Tickets.request_resolution_confirmation(
        rig.db, str(rig.ticket.id), command=command
    )
    support.Tickets.dispute_resolution(rig.db, token, reason="Still broken")
    with pytest.raises(support.SupportTicketError) as error:
        support.Tickets.request_resolution_confirmation(
            rig.db, str(rig.ticket.id), command=command
        )
    assert error.value.code == "resolution_confirmation_inactive"
    rig.refusal = "operations.field_completion.revoked"
    with pytest.raises(DomainError) as revoked:
        support.Tickets.request_resolution_confirmation(
            rig.db, str(rig.ticket.id), command=command
        )
    assert revoked.value.code == rig.refusal


def test_old_request_cannot_confirm_after_replacement(rig: GateRig) -> None:
    old = request(rig)
    request(rig)
    with pytest.raises(support.SupportTicketError) as error:
        support.Tickets.confirm_resolution(rig.db, old)
    assert error.value.code == "resolution_confirmation_inactive"
    rig.db.refresh(rig.ticket)
    assert rig.ticket.status == "pending_confirmation"


def test_expired_native_request_cannot_confirm_or_replay(rig: GateRig) -> None:
    command = SupportResolutionRequestCommand(
        context=CommandContext.system(
            actor="support-test",
            scope="support.ticket:request_resolution_confirmation",
            reason="Customer request",
            idempotency_key=str(uuid4()),
        ),
        ticket_id=rig.ticket.id,
    )
    _, token = support.Tickets.request_resolution_confirmation(
        rig.db, str(rig.ticket.id), command=command
    )
    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    rig.db.commit()
    with pytest.raises(support.SupportTicketError) as refused:
        support.Tickets.confirm_resolution(rig.db, token)
    assert refused.value.code == "resolution_confirmation_inactive"
    with pytest.raises(support.SupportTicketError) as replay:
        support.Tickets.request_resolution_confirmation(
            rig.db, str(rig.ticket.id), command=command
        )
    assert replay.value.code == "resolution_confirmation_inactive"
    rig.db.refresh(rig.ticket)
    assert rig.ticket.status == "pending_confirmation"


def test_grace_current_request_can_close_after_customer_token_expiry(
    rig: GateRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.runtime_durable_timers import fire_due_timers

    token = request(rig)
    rig.enrolled = True
    future = datetime.now(UTC) + timedelta(days=2)

    def now() -> datetime:
        return future

    monkeypatch.setattr(support, "_now", now)
    rig.db.commit()
    fired = fire_due_timers(
        rig.db,
        now=future,
        context=CommandContext.system(actor="timer", scope="timer", reason="Grace due"),
    )
    assert len(fired) == 1
    rig.db.refresh(rig.ticket)
    rig.db.refresh(token)
    assert rig.ticket.status == "closed" and not token.is_active
    attempt = rig.db.scalar(select(SupportResolutionAttempt))
    assert attempt is not None and attempt.source == "grace"


def test_ticket_only_old_timer_cannot_target_native_replacement(rig: GateRig) -> None:
    old = request(rig)
    request(rig)
    result = support.Tickets.consume_resolution_confirmation_due(
        rig.db,
        ticket_id=str(rig.ticket.id),
        event_id=uuid4(),
        context=CommandContext.system(
            actor="old-timer",
            scope="timer",
            reason="Old grace",
            idempotency_key=str(uuid4()),
        ),
    )
    assert result == "skipped_stale_request"
    rig.db.refresh(rig.ticket)
    rig.db.refresh(old)
    assert rig.ticket.status == "pending_confirmation" and not old.is_active


def test_stale_dispute_does_not_disturb_replacement_and_dispute_needs_no_acceptance(
    rig: GateRig,
) -> None:
    old = request(rig)
    current = request(rig)
    with pytest.raises(support.SupportTicketError) as refused:
        support.Tickets.dispute_resolution(rig.db, old, reason="Old response")
    assert refused.value.code == "resolution_confirmation_inactive"
    rig.db.refresh(rig.ticket)
    rig.db.refresh(current)
    assert rig.ticket.status == "pending_confirmation" and current.is_active
    rig.refusal = "operations.field_completion.revoked"
    assert (
        support.Tickets.dispute_resolution(
            rig.db, current, reason="Still broken"
        ).status
        == "open"
    )


def test_current_native_timer_not_due_and_old_native_request_are_refused(
    rig: GateRig,
) -> None:
    from app.models.durable_timer import DurableTimer, TimerStatus

    request(rig)
    native = rig.db.scalar(select(SupportResolutionRequest))
    assert native is not None
    timer = rig.db.scalar(
        select(DurableTimer).where(DurableTimer.entity_id == native.id)
    )
    assert timer is not None
    event_id = uuid4()
    timer.status = TimerStatus.fired
    timer.fired_event_id = event_id
    rig.db.commit()
    context = CommandContext.system(
        actor="timer",
        scope="timer",
        reason="Queued timer",
        idempotency_key=str(event_id),
    )
    assert (
        support.Tickets.consume_resolution_confirmation_due(
            rig.db,
            ticket_id=str(rig.ticket.id),
            event_id=event_id,
            context=context,
            timer_id=timer.id,
            timer_generation=timer.generation,
            resolution_request_id=native.id,
        )
        == "skipped_not_due"
    )
    request(rig)
    late_event = uuid4()
    timer.fired_event_id = late_event
    rig.db.commit()
    assert (
        support.Tickets.consume_resolution_confirmation_due(
            rig.db,
            ticket_id=str(rig.ticket.id),
            event_id=late_event,
            context=CommandContext.system(
                actor="timer",
                scope="timer",
                reason="Old delivery",
                idempotency_key=str(late_event),
            ),
            timer_id=timer.id,
            timer_generation=timer.generation,
            resolution_request_id=native.id,
        )
        == "skipped_stale_request"
    )
    rig.db.refresh(rig.ticket)
    assert rig.ticket.status == "pending_confirmation"


def test_sweep_skips_two_due_enrolled_tickets_before_any_completion_basis_lock(
    rig: GateRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    request(rig)
    second = Ticket(
        title="Second enrolled pending repair",
        subscriber_id=rig.ticket.subscriber_id,
        status="pending_confirmation",
        resolved_at=datetime.now(UTC) - timedelta(days=2),
        metadata_={"resolution_confirmation": {"grace_hours": 24}},
    )
    rig.db.add(second)
    rig.db.commit()
    enrolled_ids = {rig.ticket.id, second.id}
    checked: list[UUID] = []

    def presence(db: Session, ticket_id: UUID) -> bool:
        assert ticket_id in rig.locks
        checked.append(ticket_id)
        return ticket_id in enrolled_ids

    def forbidden_basis(db: Session, ticket_id: UUID) -> TicketCompletionBasis:
        raise AssertionError(
            "Sweep must not acquire completion/engineering locks for enrolled tickets"
        )

    monkeypatch.setattr(support, "_linked_completion_enrollments", presence)
    monkeypatch.setattr(support, "_ticket_completion_basis", forbidden_basis)
    assert (
        support.Tickets.auto_confirm_pending(
            rig.db, now=datetime.now(UTC) + timedelta(days=2)
        )
        == 0
    )
    assert set(checked) == enrolled_ids
    rig.db.refresh(rig.ticket)
    rig.db.refresh(second)
    assert rig.ticket.status == second.status == "pending_confirmation"
    assert (
        rig.db.scalar(select(func.count()).select_from(SupportResolutionAttempt)) == 0
    )
