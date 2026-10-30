"""Read-only prospect eligibility under communications.team_inbox_contact_resolution."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import InboxConversation
from app.services.team_inbox_contact_links import (
    IdentityEvidenceDisposition,
    conversation_identity_evidence,
)


@dataclass(frozen=True, slots=True)
class LeadIntakeResolutionQuery:
    conversation_id: UUID


@dataclass(frozen=True, slots=True)
class LeadIntakeResolutionEligibility:
    eligible: bool


def lead_intake_resolution_eligibility(
    db: Session, *, query: LeadIntakeResolutionQuery
) -> LeadIntakeResolutionEligibility:
    """Preserve unmatched eligibility; require reviewed clearance for PR 3784.

    This query cannot clear a review or create/link any identity or Lead.
    """
    conversation = db.get(InboxConversation, query.conversation_id)
    if conversation is None or conversation.subscriber_id is not None:
        return LeadIntakeResolutionEligibility(False)
    metadata = (
        conversation.metadata_ if isinstance(conversation.metadata_, dict) else {}
    )
    resolution = metadata.get("contact_resolution")
    correction = metadata.get("identity_correction")
    if not isinstance(resolution, dict):
        return LeadIntakeResolutionEligibility(False)
    if resolution.get("status") == "unmatched" and correction is None:
        return LeadIntakeResolutionEligibility(True)
    if (
        resolution.get("status") != "reviewed_no_match"
        or not isinstance(correction, dict)
        or correction.get("status") != "cleared"
    ):
        return LeadIntakeResolutionEligibility(False)
    try:
        UUID(str(correction.get("cleared_by_principal_id")))
        cleared_at = datetime.fromisoformat(str(correction.get("cleared_at")))
    except (ValueError, TypeError):
        return LeadIntakeResolutionEligibility(False)
    if cleared_at.tzinfo is None or cleared_at > datetime.now(UTC):
        return LeadIntakeResolutionEligibility(False)
    evidence = conversation_identity_evidence(db, conversation)
    return LeadIntakeResolutionEligibility(
        evidence.disposition is IdentityEvidenceDisposition.no_match
    )
