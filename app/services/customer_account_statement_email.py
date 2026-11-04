"""Customer-owned command for emailing an account statement PDF."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import CommunicationIntentRecord, NotificationChannel
from app.models.subscriber import Subscriber
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.communication_intents import (
    MAX_EMAIL_ATTACHMENT_BYTES,
    CommunicationAttachment,
    CommunicationAttachmentKind,
    CommunicationIntent,
    submit,
)
from app.services.customer_account_statement import (
    CustomerStatementError,
    CustomerStatementQuery,
    read_customer_statement,
    render_statement_pdf,
)
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


@dataclass(frozen=True, slots=True)
class CustomerStatementEmailCommand:
    query: CustomerStatementQuery
    principal_id: UUID
    idempotency_key: UUID
    context: CommandContext


@dataclass(frozen=True, slots=True)
class CustomerStatementEmailOutcome:
    intent_id: UUID
    notification_id: UUID
    replayed: bool


_EMAIL_COMMAND = OwnerCommandDefinition(
    owner="customer.account_statement_email",
    concern="customer statement PDF email request",
    name="request_customer_statement_email",
)


def request_customer_statement_email(
    db: Session, *, command: CustomerStatementEmailCommand
) -> CustomerStatementEmailOutcome:
    """Queue one attachment to the signed-in principal's verified address."""

    def operation() -> CustomerStatementEmailOutcome:
        period = command.query.period
        dedupe_key = (
            f"customer-statement:{command.query.account_id}:"
            f"{period.start_date}:{period.end_date}:{command.idempotency_key}"
        )
        existing = db.scalar(
            select(CommunicationIntentRecord).where(
                CommunicationIntentRecord.dedupe_key == dedupe_key
            )
        )
        if existing is not None:
            metadata = existing.metadata_ or {}
            if (
                existing.subscriber_id != command.principal_id
                or metadata.get("statement_account_id") != str(command.query.account_id)
                or metadata.get("statement_principal_id") != str(command.principal_id)
                or metadata.get("statement_start_date") != period.start_date.isoformat()
                or metadata.get("statement_end_date") != period.end_date.isoformat()
                or metadata.get("statement_timezone") != period.timezone.key
                or len(existing.notifications) != 1
            ):
                raise CustomerStatementError(
                    code="customer.account_statement.email_idempotency_conflict",
                    message="This statement email request cannot be replayed.",
                )
            return CustomerStatementEmailOutcome(
                intent_id=existing.id,
                notification_id=existing.notifications[0].id,
                replayed=True,
            )
        account = db.get(Subscriber, command.query.account_id)
        if account is None:
            raise CustomerStatementError(
                code="customer.account_statement.account_not_found",
                message="Account not found.",
            )
        principal = db.get(Subscriber, command.principal_id)
        if (
            principal is None
            or not principal.email_verified
            or not principal.email.strip()
        ):
            raise CustomerStatementError(
                code="customer.account_statement.email_unverified",
                message="Verify your email before emailing a statement.",
            )
        pdf = render_statement_pdf(read_customer_statement(db, query=command.query))
        if len(pdf) > MAX_EMAIL_ATTACHMENT_BYTES:
            raise CustomerStatementError(
                code="customer.account_statement.email_attachment_too_large",
                message="This statement is too large to email. Download the PDF instead.",
            )
        filename = (
            f"account-statement-{period.start_date.isoformat()}-"
            f"{period.end_date.isoformat()}.pdf"
        )
        result = submit(
            db,
            CommunicationIntent(
                subscriber_id=principal.id,
                event_type="customer.account_statement.email_requested",
                category="billing",
                subject="Your account statement",
                body=(
                    "Your account statement is attached. History before the "
                    "reviewed opening is unavailable and is not included."
                ),
                channels=(NotificationChannel.email,),
                include_reseller=False,
                resolve_subscriber_identity=False,
                recipients={NotificationChannel.email: principal.email.strip()},
                attachments=(
                    CommunicationAttachment(
                        kind=CommunicationAttachmentKind.customer_statement_pdf,
                        entity_id=account.id,
                        filename=filename,
                    ),
                ),
                metadata={
                    "statement_start_date": period.start_date.isoformat(),
                    "statement_end_date": period.end_date.isoformat(),
                    "statement_timezone": period.timezone.key,
                    "statement_account_id": str(account.id),
                    "statement_principal_id": str(principal.id),
                },
                dedupe_key=dedupe_key,
            ),
        )
        notifications = result.queued or result.deliveries
        if (not result.replayed and len(result.queued) != 1) or len(notifications) != 1:
            raise CustomerStatementError(
                code="customer.account_statement.email_unavailable",
                message="The statement email could not be queued.",
            )
        if result.replayed:
            return CustomerStatementEmailOutcome(
                intent_id=result.intent_id,
                notification_id=notifications[0].id,
                replayed=True,
            )
        stage_audit_event(
            db,
            action="customer_statement.email_queued",
            entity_type="customer_account_statement",
            entity_id=str(account.id),
            actor=AuditActor.user(str(command.principal_id)),
            metadata={
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
                "communication_intent_id": str(result.intent_id),
            },
        )
        emit_event(
            db,
            EventType.customer_account_statement_email_queued,
            {
                "account_id": str(account.id),
                "communication_intent_id": str(result.intent_id),
            },
            actor=str(command.principal_id),
        )
        db.flush()
        return CustomerStatementEmailOutcome(
            intent_id=result.intent_id,
            notification_id=notifications[0].id,
            replayed=result.replayed,
        )

    return execute_owner_command(
        db,
        definition=_EMAIL_COMMAND,
        context=command.context,
        operation=operation,
    )
