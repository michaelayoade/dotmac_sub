"""Meta Lead Ads admission and customer-match projection."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyIdentityStatus,
)
from app.models.sales import Lead, LeadCaptureMethod, LeadSourcePlatform
from app.models.subscriber import Subscriber
from app.schemas.sales import (
    LeadCapturePartyCreate,
    LeadCaptureRequest,
    LeadContactObservation,
    LeadOriginCaptureCreate,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.integrations.meta_social_contracts import MetaLeadObservation
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales.capture import LeadCaptureResult, capture_verified_receipt

META_LEAD_MATCH_SCOPE = "sales:reconcile-meta-lead-customer-match"
_RECONCILE_MATCH = OwnerCommandDefinition(
    owner="sales.meta_lead_customer_match",
    concern="Meta Lead customer-match projection",
    name="reconcile_meta_lead_customer_match",
)
logger = logging.getLogger(__name__)


class MetaLeadMatchStatus(StrEnum):
    unmatched = "unmatched"
    single_candidate = "single_candidate"
    ambiguous = "ambiguous"
    unavailable = "unavailable"


class MetaLeadAdsError(DomainError):
    """Stable failure at the Meta-specific lead admission boundary."""


@dataclass(frozen=True, slots=True)
class MetaLeadCustomerMatch:
    status: MetaLeadMatchStatus
    subscriber_ids: tuple[UUID, ...]
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class MetaLeadCaptureOutcome:
    lead_id: UUID
    party_id: UUID
    replayed: bool
    customer_match: MetaLeadCustomerMatch


@dataclass(frozen=True, slots=True)
class ReconcileMetaLeadMatchCommand:
    context: CommandContext
    lead_id: UUID


def _error(suffix: str, message: str, **details: object) -> MetaLeadAdsError:
    return MetaLeadAdsError(
        code=f"sales.meta_lead_ads.{suffix}", message=message, details=details
    )


def _field_values(observation: MetaLeadObservation) -> dict[str, tuple[str, ...]]:
    return {field.name.lower(): field.values for field in observation.fields}


def _first(fields: dict[str, tuple[str, ...]], *names: str) -> str | None:
    for name in names:
        values = fields.get(name)
        if values:
            value = values[0].strip()
            if value:
                return value
    return None


def _capture_request(observation: MetaLeadObservation) -> LeadCaptureRequest:
    fields = _field_values(observation)
    full_name = _first(fields, "full_name")
    if not full_name:
        full_name = " ".join(
            part
            for part in (
                _first(fields, "first_name"),
                _first(fields, "last_name"),
            )
            if part
        ).strip()
    display_name = full_name or f"Meta Lead {observation.leadgen_id[-8:]}"
    contacts: list[LeadContactObservation] = []
    email = _first(fields, "email")
    phone = _first(fields, "phone_number", "phone")
    if email:
        contacts.append(
            LeadContactObservation(
                channel_type=PartyContactPointType.email,
                value=email,
                display_value=email,
                provider="meta",
            )
        )
    if phone:
        contacts.append(
            LeadContactObservation(
                channel_type=PartyContactPointType.phone,
                value=phone,
                display_value=phone,
                provider="meta",
            )
        )
    address = _first(fields, "street_address")
    region = _first(fields, "state", "region")
    city = _first(fields, "city")
    if city:
        address = ", ".join(part for part in (address, city) if part)
    return LeadCaptureRequest(
        party=LeadCapturePartyCreate(
            display_name=display_name,
            contacts=contacts,
        ),
        title=f"Meta Lead - {display_name}"[:200],
        lead_source="Facebook Ads",
        origin=LeadOriginCaptureCreate(
            capture_method=LeadCaptureMethod.ad_lead_form_webhook,
            source_platform=LeadSourcePlatform.meta,
            source_interaction_id=observation.leadgen_id,
            external_campaign_id=observation.campaign_id,
            external_ad_set_id=observation.ad_set_id,
            external_ad_id=observation.ad_id,
            external_form_id=observation.form_id,
            captured_at=observation.created_at,
            capture_source="meta.lead_ads_webhook",
            capture_reason="Verified Meta Lead Ads webhook and Graph retrieval",
        ),
        region=region,
        address=address,
        notes=None,
    )


def _match_evidence(db: Session, *, lead: Lead) -> MetaLeadCustomerMatch:
    if lead.party_id is None:
        raise _error("lead_party_missing", "The captured Lead has no Party identity.")
    observed = tuple(
        db.scalars(
            select(PartyContactPoint).where(
                PartyContactPoint.party_id == lead.party_id,
                PartyContactPoint.is_active.is_(True),
            )
        ).all()
    )
    candidate_ids: set[UUID] = set()
    evidence: list[tuple[str, str]] = []
    for point in observed:
        matches = db.execute(
            select(Subscriber.id, PartyContactPoint.id)
            .join(PartyContactPoint, PartyContactPoint.party_id == Subscriber.party_id)
            .join(Party, Party.id == PartyContactPoint.party_id)
            .where(
                PartyContactPoint.channel_type == point.channel_type,
                PartyContactPoint.normalized_value == point.normalized_value,
                PartyContactPoint.verification_status
                == PartyContactVerificationStatus.verified.value,
                PartyContactPoint.is_active.is_(True),
                Subscriber.is_active.is_(True),
                Subscriber.party_id.is_not(None),
                PartyContactPoint.party_id != lead.party_id,
                Party.status == PartyIdentityStatus.active.value,
            )
        ).all()
        for subscriber_id, contact_point_id in matches:
            candidate_ids.add(subscriber_id)
            evidence.append((str(contact_point_id), point.channel_type))
    ordered = tuple(sorted(candidate_ids, key=str))
    status = (
        MetaLeadMatchStatus.unmatched
        if not ordered
        else MetaLeadMatchStatus.single_candidate
        if len(ordered) == 1
        else MetaLeadMatchStatus.ambiguous
    )
    fingerprint = hashlib.sha256(
        json.dumps(sorted(evidence), separators=(",", ":")).encode()
    ).hexdigest()
    return MetaLeadCustomerMatch(
        status=status,
        subscriber_ids=ordered,
        evidence_fingerprint=fingerprint,
    )


def reconcile_customer_match(
    db: Session, command: ReconcileMetaLeadMatchCommand
) -> MetaLeadCustomerMatch:
    def operation() -> MetaLeadCustomerMatch:
        if command.context.scope != META_LEAD_MATCH_SCOPE:
            raise _error("scope_invalid", "Meta Lead matching requires its own scope.")
        lead = db.scalars(
            select(Lead).where(Lead.id == command.lead_id).with_for_update()
        ).one_or_none()
        if lead is None:
            raise _error("lead_not_found", "Meta Lead was not found.")
        match = _match_evidence(db, lead=lead)
        metadata = dict(lead.metadata_ or {})
        metadata["meta_customer_match"] = {
            "status": match.status.value,
            "subscriber_ids": [str(value) for value in match.subscriber_ids],
            "evidence_fingerprint": match.evidence_fingerprint,
        }
        lead.metadata_ = metadata
        emit_event(
            db,
            EventType.meta_lead_customer_match_reconciled,
            {
                "lead_id": str(lead.id),
                "status": match.status.value,
                "candidate_count": len(match.subscriber_ids),
                "evidence_fingerprint": match.evidence_fingerprint,
            },
            actor=command.context.actor,
            subscriber_id=lead.subscriber_id,
        )
        db.flush()
        return match

    return execute_owner_command(
        db,
        definition=_RECONCILE_MATCH,
        context=command.context,
        operation=operation,
    )


def capture_meta_lead(
    db: Session,
    *,
    receipt_id: UUID,
    observation: MetaLeadObservation,
) -> MetaLeadCaptureOutcome:
    result: LeadCaptureResult = capture_verified_receipt(
        db,
        receipt_id=receipt_id,
        payload=_capture_request(observation),
        actor_id="integration.meta_lead_ads",
    )
    lead_id = result.lead.id
    party_id = result.party_id
    replayed = result.replayed
    finish_read_transaction(db)
    try:
        match = reconcile_customer_match(
            db,
            ReconcileMetaLeadMatchCommand(
                context=CommandContext.system(
                    actor="integration.meta_lead_ads",
                    scope=META_LEAD_MATCH_SCOPE,
                    reason="Refresh customer candidates from verified contact points",
                    idempotency_key=f"meta-lead-match:{lead_id}",
                ),
                lead_id=lead_id,
            ),
        )
    except Exception:
        logger.exception(
            "meta_lead_customer_match_failed",
            extra={"lead_id": str(lead_id)},
        )
        match = MetaLeadCustomerMatch(
            status=MetaLeadMatchStatus.unavailable,
            subscriber_ids=(),
            evidence_fingerprint="",
        )
    return MetaLeadCaptureOutcome(
        lead_id=lead_id,
        party_id=party_id,
        replayed=replayed,
        customer_match=match,
    )
