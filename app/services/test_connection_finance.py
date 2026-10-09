"""Owner of Finance review consequences for classified temporary test requests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.automation import AutomationRuleVersion
from app.models.event_store import EventStore
from app.models.service_team import ServiceTeam
from app.models.subscriber import Subscriber
from app.models.test_connection import TestConnectionGrant
from app.models.test_connection_review import TestConnectionFinanceReview
from app.schemas.test_connection import (
    TestConnectionCreated,
    TestConnectionFinanceReviewQueued,
)
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.staff_notifications import (
    StaffDirectEventType,
    StageStaffDirectNotification,
    resolve_assignment_users,
    stage_staff_direct_notification,
)

OWNER = "financial.test_connection_finance_review"
CONCERN = "temporary Test Connection Finance review notifications"
_NOTIFY = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="notify_test_connection_finance"
)


class TestConnectionFinanceError(DomainError):
    """Invalid source evidence, recipient configuration, or replay identity."""


def _error(code: str, message: str) -> TestConnectionFinanceError:
    return TestConnectionFinanceError(
        code=f"{OWNER}.{code}",
        message=message,
        retryable=code == "recipients_unavailable",
    )


@dataclass(frozen=True, slots=True)
class NotifyTestConnectionFinanceCommand:
    context: CommandContext
    tenant_id: UUID
    event_id: UUID
    grant_id: UUID
    rule_version_id: UUID
    step_index: int
    service_team_id: UUID


@dataclass(frozen=True, slots=True)
class TestConnectionFinanceOutcome:
    review_id: UUID
    recipient_ids: tuple[UUID, ...]
    replayed: bool


def notify_test_connection_finance(
    db: Session,
    command: NotifyTestConnectionFinanceCommand,
) -> TestConnectionFinanceOutcome:
    """Validate the durable event, freeze recipients, and stage both channels."""

    def operation() -> TestConnectionFinanceOutcome:
        if (
            command.context.scope != "automation:runtime"
            or not command.context.idempotency_key
            or command.tenant_id != OPERATOR_TENANT_ID
            or command.step_index < 0
        ):
            raise _error(
                "invalid_scope",
                "The Finance action requires an authorized operator workflow.",
            )
        event = db.scalar(
            select(EventStore)
            .where(EventStore.event_id == command.event_id)
            .with_for_update()
        )
        if event is None or event.event_type != EventType.test_connection_created.value:
            raise _error(
                "invalid_evidence",
                "The source event is not a Test Connection creation.",
            )
        try:
            evidence = TestConnectionCreated.model_validate(event.payload)
        except ValidationError as exc:
            raise _error(
                "invalid_evidence", "The Test Connection event evidence is invalid."
            ) from exc
        version = db.get(AutomationRuleVersion, command.rule_version_id)
        if (
            version is None
            or version.tenant_id != command.tenant_id
            or version.published_at is None
        ):
            raise _error(
                "invalid_evidence",
                "The Finance action requires a published operator workflow version.",
            )
        grant = db.get(TestConnectionGrant, command.grant_id)
        if (
            evidence.tenant_id != command.tenant_id
            or evidence.grant_id != command.grant_id
            or event.account_id != evidence.customer_id
            or grant is None
            or grant.subscriber_id != evidence.customer_id
            or grant.subscription_id != evidence.subscription_id
            or event.subscription_id != evidence.subscription_id
        ):
            raise _error(
                "invalid_evidence",
                "The event does not match the customer Test Connection grant.",
            )
        digest = hashlib.sha256(evidence.model_dump_json().encode()).hexdigest()
        review_id = uuid5(
            NAMESPACE_URL,
            f"{OWNER}:{command.event_id}:{command.rule_version_id}:{command.step_index}",
        )
        existing = db.get(TestConnectionFinanceReview, review_id)
        if existing is not None:
            if (
                existing.service_team_id != command.service_team_id
                or existing.payload_sha256 != digest
            ):
                raise _error(
                    "replay_conflict",
                    "This workflow action was already applied with different evidence.",
                )
            return TestConnectionFinanceOutcome(
                review_id, tuple(UUID(item) for item in existing.recipient_ids), True
            )
        steps = tuple(
            step
            for step in version.actions
            if step.get("position") == command.step_index
            and step.get("action_key") == "billing.test_connection.notify_finance"
        )
        if len(steps) != 1:
            raise _error(
                "invalid_evidence",
                "The published workflow does not authorize this Finance action.",
            )
        stored_inputs = steps[0].get("inputs")
        if not isinstance(stored_inputs, list):
            raise _error(
                "invalid_evidence", "The published Finance action inputs are invalid."
            )
        configured_team = tuple(
            item.get("value")
            for item in stored_inputs
            if isinstance(item, dict) and item.get("key") == "service_team_id"
        )
        if configured_team != (str(command.service_team_id),):
            raise _error(
                "invalid_evidence",
                "The recipient team differs from the published workflow.",
            )
        team = db.get(ServiceTeam, command.service_team_id)
        if team is None or not team.is_active:
            raise _error(
                "recipients_unavailable", "The configured Finance team is unavailable."
            )
        users = resolve_assignment_users(
            db, service_team_ids=(str(command.service_team_id),)
        )
        if not users or any(not (user.email or "").strip() for user in users):
            raise _error(
                "recipients_unavailable",
                "The Finance team needs active staff recipients with email addresses.",
            )
        customer = db.get(Subscriber, evidence.customer_id)
        if customer is None:
            raise _error(
                "invalid_evidence",
                "The customer for this Test Connection is unavailable.",
            )
        label = (
            customer.display_name
            or " ".join(filter(None, (customer.first_name, customer.last_name)))
            or customer.account_number
            or str(customer.id)
        )
        subject = f"Review repeated Test Connections: {label}"
        body = (
            f"Customer: {label}\nAccount: {customer.account_number or customer.id}\n"
            f"Test Connections created: {evidence.count_7d}\n"
            f"7-day window (UTC): after {evidence.window_start.isoformat()} through {evidence.window_end.isoformat()}\n"
            "Please review repeated temporary service requests for possible misuse. "
            "This is a review alert, not a finding of wrongdoing. Counts include expired or ended Test Connections.\n\n"
            "Recent request references:\n"
            + "\n".join(
                f"{item.grant_id} | {item.created_at.isoformat()} | {item.duration_seconds // 3600} hour(s) | created by {item.created_by or 'unknown'}"
                for item in evidence.recent_connections
            )
        )
        for user in users:
            stage_staff_direct_notification(
                db,
                StageStaffDirectNotification(
                    system_user_id=user.id,
                    source_event_id=review_id,
                    event_type=StaffDirectEventType.test_connection_finance_review,
                    subject=subject,
                    body=body,
                    target_url=f"/admin/catalog/subscriptions/{evidence.subscription_id}",
                ),
            )
        recipient_ids = tuple(user.id for user in users)
        db.add(
            TestConnectionFinanceReview(
                id=review_id,
                event_id=command.event_id,
                rule_version_id=command.rule_version_id,
                step_index=command.step_index,
                service_team_id=command.service_team_id,
                recipient_ids=[str(item) for item in recipient_ids],
                payload_sha256=digest,
            )
        )
        db.flush()
        stage_audit_event(
            db,
            action="billing.test_connection_finance_review_queued",
            entity_type="test_connection_finance_review",
            entity_id=str(review_id),
            actor=AuditActor.system(command.context.actor),
            request_id=str(command.context.correlation_id),
            metadata={
                "source_event_id": str(command.event_id),
                "rule_version_id": str(command.rule_version_id),
                "step_index": command.step_index,
                "recipient_count": len(recipient_ids),
                "count_7d": evidence.count_7d,
            },
        )
        queued = TestConnectionFinanceReviewQueued(
            tenant_id=command.tenant_id,
            review_id=review_id,
            source_event_id=command.event_id,
            customer_id=evidence.customer_id,
            rule_version_id=command.rule_version_id,
            step_index=command.step_index,
            recipient_count=len(recipient_ids),
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
            causation_id=command.context.causation_id,
        )
        emit_event(
            db,
            EventType.test_connection_finance_review_queued,
            queued.model_dump(mode="json"),
            event_id=uuid5(NAMESPACE_URL, f"{OWNER}:queued:{review_id}"),
            actor=command.context.actor,
            account_id=evidence.customer_id,
            dispatch_after_commit=False,
        )
        return TestConnectionFinanceOutcome(review_id, recipient_ids, False)

    return execute_owner_command(
        db, definition=_NOTIFY, context=command.context, operation=operation
    )
