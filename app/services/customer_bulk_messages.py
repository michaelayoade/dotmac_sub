"""Durable admission and materialization owner for customer bulk sends.

The scoped SystemJob row is also the dispatch outbox. Broker availability is
not an admission decision: the permanent drain retries accepted work.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.notification import Notification, NotificationStatus
from app.models.system_job import SystemJob
from app.services.customer_bulk_message_contracts import (
    BulkMessageEvaluation,
    BulkMessageSpec,
    BulkSendReceipt,
    BulkSendState,
    BulkSendStatus,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

JOB_TYPE = "customer_bulk_message"
OWNER = "communications.customer_bulk_messages"
CONCERN = "durable customer bulk message admission and materialization"
LEASE = timedelta(minutes=15)
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class AcceptBulkMessageCommand:
    context: CommandContext
    request_id: UUID
    actor_id: UUID
    spec: BulkMessageSpec


@dataclass(frozen=True)
class ProcessBulkMessageCommand:
    context: CommandContext
    request_id: UUID
    attempt: int | None = None


@dataclass(frozen=True)
class RecordBulkMessageFailureCommand:
    context: CommandContext
    request_id: UUID
    attempt: int
    retryable: bool
    message: str


@dataclass(frozen=True)
class BulkMessageStatusQuery:
    request_id: UUID
    actor_id: UUID


@dataclass(frozen=True)
class DueBulkMessagesQuery:
    now: datetime
    limit: int = 20


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name=name)


def _error(suffix: str, message: str) -> DomainError:
    return DomainError(code=f"{OWNER}.{suffix}", message=message, retryable=False)


def _receipt(row: SystemJob) -> BulkSendReceipt:
    return BulkSendReceipt.model_validate(row.payload_json)


def _store(db: Session, row: SystemJob, receipt: BulkSendReceipt) -> None:
    row.payload_json = receipt.model_dump(mode="json")
    row.status = receipt.state.value
    row.error = receipt.error
    row.updated_at = datetime.now(UTC)
    db.flush()
    emit_event(
        db,
        EventType.customer_bulk_message_changed,
        {
            "request_id": str(receipt.request_id),
            "state": receipt.state.value,
            "attempt": receipt.attempts,
            "schema_version": 1,
        },
        actor=str(receipt.actor_id),
        record_only=True,
    )


def _locked(db: Session, request_id: UUID) -> SystemJob | None:
    return db.scalar(
        select(SystemJob)
        .where(SystemJob.job_type == JOB_TYPE, SystemJob.job_id == str(request_id))
        .with_for_update(skip_locked=True)
    )


def accept(db: Session, *, command: AcceptBulkMessageCommand) -> BulkSendReceipt:
    def operation() -> BulkSendReceipt:
        if not command.spec.confirmed or command.spec.preview_only:
            raise _error(
                "invalid_command", "Preview and confirm the message before sending."
            )
        # Replay is checked before re-evaluating current audience or template facts.
        fingerprint = sha256(
            json.dumps(
                command.spec.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        candidate = BulkSendReceipt(
            request_id=command.request_id,
            actor_id=command.actor_id,
            fingerprint=fingerprint,
            spec=command.spec,
            accepted_at=now,
        )
        # Existing migrated uniqueness arbitrates simultaneous HTTP retries.
        db.execute(
            insert(SystemJob)
            .values(
                job_type=JOB_TYPE,
                job_id=str(command.request_id),
                status="accepted",
                owner_actor_id=str(command.actor_id),
                queued_at=now,
                payload_json=candidate.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=["job_type", "job_id"])
        )
        row = db.scalar(
            select(SystemJob)
            .where(
                SystemJob.job_type == JOB_TYPE,
                SystemJob.job_id == str(command.request_id),
            )
            .with_for_update()
        )
        if row is None:
            raise _error("not_found", "Bulk send request not found.")
        receipt = _receipt(row)
        if receipt.actor_id != command.actor_id:
            raise _error("not_found", "Bulk send request not found.")
        if receipt.fingerprint != fingerprint:
            raise _error(
                "idempotency_conflict",
                "This send reference was used for a different message.",
            )
        if receipt.counts.matched_count:
            return receipt
        from app.services.web_customer_actions import preview_bulk_message

        counts = preview_bulk_message(db=db, spec=command.spec)
        receipt = receipt.model_copy(update={"counts": counts})
        _store(db, row, receipt)
        return receipt

    return execute_owner_command(
        db,
        definition=_definition("accept"),
        context=command.context,
        operation=operation,
    )


def claim(db: Session, *, command: ProcessBulkMessageCommand) -> BulkSendReceipt | None:
    def operation() -> BulkSendReceipt | None:
        row = _locked(db, command.request_id)
        if row is None:
            return None
        receipt = _receipt(row)
        now = datetime.now(UTC)
        if receipt.state in {BulkSendState.queued, BulkSendState.failed}:
            return None
        started = row.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        due = row.queued_at
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if (
            receipt.state == BulkSendState.preparing
            and started
            and started > now - LEASE
        ):
            return None
        if receipt.state == BulkSendState.accepted and due and due > now:
            return None
        if receipt.attempts >= MAX_ATTEMPTS:
            receipt = receipt.model_copy(
                update={
                    "state": BulkSendState.failed,
                    "error": "Recipient preparation stopped after repeated worker failures.",
                }
            )
            row.completed_at = now
            _store(db, row, receipt)
            return None
        receipt = receipt.model_copy(
            update={"state": BulkSendState.preparing, "attempts": receipt.attempts + 1}
        )
        row.started_at = now
        _store(db, row, receipt)
        return receipt

    return execute_owner_command(
        db,
        definition=_definition("claim"),
        context=command.context,
        operation=operation,
    )


def materialize(
    db: Session, *, command: ProcessBulkMessageCommand
) -> BulkSendReceipt | None:
    def operation() -> BulkSendReceipt | None:
        row = _locked(db, command.request_id)
        if row is None:
            return None
        receipt = _receipt(row)
        if (
            receipt.state != BulkSendState.preparing
            or receipt.attempts != command.attempt
        ):
            return None
        from app.services.web_customer_actions import materialize_bulk_message

        counts = materialize_bulk_message(db=db, spec=receipt.spec)
        receipt = receipt.model_copy(
            update={"state": BulkSendState.queued, "counts": counts, "error": None}
        )
        row.completed_at = datetime.now(UTC)
        _store(db, row, receipt)
        return receipt

    return execute_owner_command(
        db,
        definition=_definition("materialize"),
        context=command.context,
        operation=operation,
    )


def record_failure(db: Session, *, command: RecordBulkMessageFailureCommand) -> None:
    def operation() -> None:
        row = _locked(db, command.request_id)
        if row is None:
            return
        receipt = _receipt(row)
        if (
            receipt.state != BulkSendState.preparing
            or receipt.attempts != command.attempt
        ):
            return
        retry = command.retryable and receipt.attempts < MAX_ATTEMPTS
        receipt = receipt.model_copy(
            update={
                "state": BulkSendState.accepted if retry else BulkSendState.failed,
                "error": command.message,
            }
        )
        row.queued_at = datetime.now(UTC) + timedelta(minutes=receipt.attempts)
        if not retry:
            row.completed_at = datetime.now(UTC)
        _store(db, row, receipt)

    execute_owner_command(
        db,
        definition=_definition("record_failure"),
        context=command.context,
        operation=operation,
    )


@dataclass(frozen=True)
class ImmediateBulkMessageCommand:
    """Compatibility command for existing internal callers; HTTP uses receipts."""

    context: CommandContext
    spec: BulkMessageSpec


def materialize_immediate(
    db: Session, *, command: ImmediateBulkMessageCommand
) -> BulkMessageEvaluation:
    from app.services.web_customer_actions import evaluate_bulk_message

    def operation() -> BulkMessageEvaluation:
        return evaluate_bulk_message(db=db, spec=command.spec)

    return execute_owner_command(
        db,
        definition=_definition("materialize_immediate"),
        context=command.context,
        operation=operation,
    )


def due_requests(db: Session, *, query: DueBulkMessagesQuery) -> tuple[UUID, ...]:
    rows = db.scalars(
        select(SystemJob.job_id)
        .where(
            SystemJob.job_type == JOB_TYPE,
            or_(
                (SystemJob.status == "accepted") & (SystemJob.queued_at <= query.now),
                (SystemJob.status == "preparing")
                & (SystemJob.started_at <= query.now - LEASE),
            ),
        )
        .order_by(SystemJob.queued_at)
        .limit(min(max(query.limit, 1), 100))
    ).all()
    return tuple(UUID(value) for value in rows)


def status(db: Session, *, query: BulkMessageStatusQuery) -> BulkSendStatus:
    row = db.scalar(
        select(SystemJob).where(
            SystemJob.job_type == JOB_TYPE,
            SystemJob.job_id == str(query.request_id),
            SystemJob.owner_actor_id == str(query.actor_id),
        )
    )
    if row is None:
        raise _error("not_found", "Bulk send request not found.")
    receipt = _receipt(row)
    counts = receipt.counts
    delivered = submitted = pending = failed = canceled = 0
    if counts.notification_ids:
        groups = db.execute(
            select(Notification.status, func.count())
            .where(
                Notification.id.in_(counts.notification_ids),
            )
            .group_by(Notification.status)
        ).all()
        for delivery_state, count in groups:
            if delivery_state == NotificationStatus.delivered:
                delivered += count
            elif delivery_state == NotificationStatus.submitted:
                submitted += count
            elif delivery_state in {
                NotificationStatus.queued,
                NotificationStatus.sending,
            }:
                pending += count
            elif delivery_state == NotificationStatus.failed:
                # Failed attempts with send_at still set remain retryable.
                retrying = (
                    db.scalar(
                        select(func.count())
                        .select_from(Notification)
                        .where(
                            Notification.id.in_(counts.notification_ids),
                            Notification.status == NotificationStatus.failed,
                            Notification.send_at.is_not(None),
                        )
                    )
                    or 0
                )
                pending += retrying
                failed += count - retrying
            elif delivery_state == NotificationStatus.bounced:
                failed += count
            else:
                canceled += count
    return BulkSendStatus(
        request_id=query.request_id,
        materialization_status=receipt.state,
        matched_count=counts.matched_count,
        planned_queued_count=counts.queued_count,
        planned_suppressed_count=counts.suppressed_count,
        skipped_count=counts.skipped_count,
        delivered_count=delivered,
        submitted_count=submitted,
        pending_count=pending,
        failed_count=failed,
        canceled_count=canceled,
        error=receipt.error,
        status_url=f"/admin/customers/bulk/send-message/{query.request_id}",
    )
