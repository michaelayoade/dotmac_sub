"""Fast unit behavior; deployed-schema/concurrency evidence lives in integration."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.system_job import SystemJob
from app.services import customer_bulk_messages as owner
from app.services.customer_bulk_message_contracts import (
    BulkMessageCounts,
    BulkMessageSpec,
    BulkSendState,
    CustomerMessageSelection,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _command():
    request_id = uuid4()
    return owner.AcceptBulkMessageCommand(
        context=CommandContext.system(
            actor="test:bulk_send", scope=str(request_id), reason="test confirmed send"
        ),
        request_id=request_id,
        actor_id=uuid4(),
        spec=BulkMessageSpec(
            channel="email",
            template_id=uuid4(),
            confirmed=True,
            expected_impact_token="preview-impact",
            selection=CustomerMessageSelection(
                mode="selected",
                ids=(uuid4(),),
                expected_count=1,
                expected_scope_token="preview-scope",
            ),
        ),
    )


def _accept(db_session, monkeypatch, command):
    from app.services import customer_bulk_message_evaluation

    monkeypatch.setattr(
        customer_bulk_message_evaluation,
        "preview_bulk_message",
        lambda *, db, spec: BulkMessageCounts(
            matched_count=1, created_count=1, queued_count=1
        ),
    )
    db_session_adapter.release_read_transaction(db_session)
    return owner.accept(db=db_session, command=command)


def test_replay_returns_receipt_without_revalidating_changed_live_facts(
    db_session, monkeypatch
):
    command = _command()
    first = _accept(db_session, monkeypatch, command)
    from app.services import customer_bulk_message_evaluation

    def reject_live_preview(**_kwargs):
        raise AssertionError("Replay must not recalculate current impact")

    monkeypatch.setattr(
        customer_bulk_message_evaluation, "preview_bulk_message", reject_live_preview
    )
    second = owner.accept(db=db_session, command=command)
    assert first == second
    assert db_session.scalar(
        select(SystemJob).where(SystemJob.job_type == owner.JOB_TYPE)
    ).job_id == str(command.request_id)


def test_changed_request_and_other_actor_fail_closed(db_session, monkeypatch):
    command = _command()
    _accept(db_session, monkeypatch, command)
    changed = owner.AcceptBulkMessageCommand(
        context=command.context,
        request_id=command.request_id,
        actor_id=command.actor_id,
        spec=command.spec.model_copy(update={"template_id": uuid4()}),
    )
    with pytest.raises(DomainError, match="different message"):
        owner.accept(db=db_session, command=changed)
    with pytest.raises(DomainError, match="not found"):
        owner.status(
            db=db_session,
            query=owner.BulkMessageStatusQuery(
                request_id=command.request_id, actor_id=uuid4()
            ),
        )


def test_receipt_outbox_remains_due_without_a_broker(db_session, monkeypatch):
    command = _command()
    _accept(db_session, monkeypatch, command)
    due = owner.due_requests(
        db=db_session, query=owner.DueBulkMessagesQuery(now=datetime.now(UTC))
    )
    assert due == (command.request_id,)


def test_claim_and_materialization_replay_are_noops(db_session, monkeypatch):
    command = _command()
    _accept(db_session, monkeypatch, command)
    process = owner.ProcessBulkMessageCommand(
        context=command.context, request_id=command.request_id
    )
    claim = owner.claim(db=db_session, command=process)
    assert claim.state == BulkSendState.preparing
    assert owner.claim(db=db_session, command=process) is None
    from app.services import customer_bulk_message_evaluation

    calls = []
    monkeypatch.setattr(
        customer_bulk_message_evaluation,
        "materialize_bulk_message",
        lambda *, db, spec: (
            calls.append(spec)
            or BulkMessageCounts(matched_count=1, created_count=1, queued_count=1)
        ),
    )
    completed = owner.materialize(
        db=db_session,
        command=owner.ProcessBulkMessageCommand(
            context=command.context,
            request_id=command.request_id,
            attempt=claim.attempts,
        ),
    )
    assert completed.state == BulkSendState.queued
    assert owner.claim(db=db_session, command=process) is None
    assert len(calls) == 1


def test_preparation_rollback_precedes_durable_failure(db_session, monkeypatch):
    command = _command()
    _accept(db_session, monkeypatch, command)
    claim = owner.claim(
        db=db_session,
        command=owner.ProcessBulkMessageCommand(
            context=command.context, request_id=command.request_id
        ),
    )
    from app.services import customer_bulk_message_evaluation

    def fail_participant(*, db, spec):
        db.add(
            SystemJob(
                job_type="rollback_probe",
                job_id=str(command.request_id),
                status="queued",
            )
        )
        db.flush()
        raise DomainError(
            code="communications.customer_bulk_messages.impact_changed",
            message="Preview changed",
            retryable=False,
        )

    monkeypatch.setattr(
        customer_bulk_message_evaluation, "materialize_bulk_message", fail_participant
    )
    with pytest.raises(DomainError):
        owner.materialize(
            db=db_session,
            command=owner.ProcessBulkMessageCommand(
                context=command.context,
                request_id=command.request_id,
                attempt=claim.attempts,
            ),
        )
    owner.record_failure(
        db=db_session,
        command=owner.RecordBulkMessageFailureCommand(
            context=command.context,
            request_id=command.request_id,
            attempt=claim.attempts,
            retryable=False,
            message="Preview changed",
        ),
    )
    assert (
        db_session.scalar(
            select(SystemJob).where(SystemJob.job_type == "rollback_probe")
        )
        is None
    )
    status = owner.status(
        db=db_session,
        query=owner.BulkMessageStatusQuery(
            request_id=command.request_id, actor_id=command.actor_id
        ),
    )
    assert status.materialization_status == BulkSendState.failed
    assert status.error == "Preview changed"


def test_stale_claim_becomes_due_for_bounded_recovery(db_session, monkeypatch):
    command = _command()
    _accept(db_session, monkeypatch, command)
    owner.claim(
        db=db_session,
        command=owner.ProcessBulkMessageCommand(
            context=command.context, request_id=command.request_id
        ),
    )
    due = owner.due_requests(
        db=db_session,
        query=owner.DueBulkMessagesQuery(now=datetime.now(UTC) + timedelta(minutes=16)),
    )
    assert due == (command.request_id,)
