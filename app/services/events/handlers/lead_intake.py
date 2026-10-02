"""Durable Sales consequence for final AI lead-candidate classifications."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.schemas.lead_intake import AiLeadCandidateClassifiedEvent
from app.services import lead_intake_ai
from app.services.domain_errors import DomainError
from app.services.events.types import Event, EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID

HANDLED_EVENT_TYPES = frozenset({EventType.ai_intake_lead_candidate_classified})


class LeadIntakeEventError(DomainError):
    """Permanent refusal of an invalid classified-candidate event."""


class LeadIntakeHandler:
    """Submit one typed, idempotent classification to the Sales owner."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        payload = AiLeadCandidateClassifiedEvent.model_validate(event.payload)
        if payload.tenant_id != OPERATOR_TENANT_ID:
            raise LeadIntakeEventError(
                code="sales.lead_intake.event_tenant_mismatch",
                message="Lead intake event tenant does not match the operator tenant.",
                retryable=False,
            )
        finish_read_transaction(db)
        lead_intake_ai.apply_shared_classification(
            db,
            conversation_id=payload.conversation_id,
            message_id=payload.message_id,
            classification=payload.classification,
            provider_label=payload.provider_label,
            model_label=payload.model_label,
            attribution=payload.attribution,
        )
