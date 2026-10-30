"""Read-only resolution of signed website provenance for WhatsApp Leads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxMessage,
    InboxProviderObservation,
)
from app.schemas.fiber_inquiry import FiberAttribution
from app.schemas.website_handoff import WebsiteHandoffAttribution
from app.services.customer_identity_normalization import default_country_code
from app.services.website_handoff_values import (
    WebsiteHandoffFact,
    WhatsAppHandoffFact,
    match_handoff,
    reference_from_body,
)


@dataclass(frozen=True, slots=True)
class WebsiteHandoffQuery:
    whatsapp_observation_id: UUID
    expected_message_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class WebsiteHandoffMessageQuery:
    message_id: UUID


@dataclass(frozen=True, slots=True)
class WebsiteHandoffConversationQuery:
    conversation_id: UUID
    message_id: UUID | None = None


def resolve_for_conversation(
    db: Session, *, query: WebsiteHandoffConversationQuery
) -> WebsiteHandoffAttribution | None:
    conversation = db.get(InboxConversation, query.conversation_id)
    if conversation is None or conversation.channel_type != "whatsapp":
        return None
    statement = select(InboxMessage).where(
        InboxMessage.conversation_id == query.conversation_id,
        InboxMessage.channel_type == "whatsapp",
        InboxMessage.direction == "inbound",
    )
    if query.message_id is not None:
        statement = statement.where(InboxMessage.id == query.message_id)
    message = db.scalar(
        statement.order_by(
            InboxMessage.created_at.desc(), InboxMessage.id.desc()
        ).limit(1)
    )
    if message is None:
        return None
    return resolve_for_message(db, query=WebsiteHandoffMessageQuery(message.id))


def resolve_for_message(
    db: Session, *, query: WebsiteHandoffMessageQuery
) -> WebsiteHandoffAttribution | None:
    message = db.get(InboxMessage, query.message_id)
    if message is None or message.channel_type != "whatsapp":
        return None
    cutoff = message.received_at or message.created_at
    if cutoff is None:
        return None
    observations = list(
        db.scalars(
            select(InboxProviderObservation)
            .join(InboxMessage, InboxProviderObservation.message_id == InboxMessage.id)
            .where(
                InboxMessage.conversation_id == message.conversation_id,
                InboxProviderObservation.provider == "meta_cloud_api",
                InboxProviderObservation.channel_type == "whatsapp",
                InboxProviderObservation.observed_at <= cutoff,
                InboxProviderObservation.observed_at >= cutoff - timedelta(days=30),
                InboxProviderObservation.normalized_payload["body"]
                .as_string()
                .contains("FWA-"),
            )
            .order_by(InboxProviderObservation.observed_at, InboxProviderObservation.id)
            .limit(201)
        ).all()
    )
    if len(observations) > 200:
        return None
    matches: dict[UUID, WebsiteHandoffAttribution] = {}
    for observed in observations:
        found = resolve(db, query=WebsiteHandoffQuery(observed.id, observed.message_id))
        if found is not None:
            matches[found.observation_id] = found
    return next(iter(matches.values())) if len(matches) == 1 else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def resolve(
    db: Session, *, query: WebsiteHandoffQuery
) -> WebsiteHandoffAttribution | None:
    """Return one exact, phone/account-bound match; never write or create Leads."""
    incoming = db.get(InboxProviderObservation, query.whatsapp_observation_id)
    if (
        incoming is None
        or incoming.provider != "meta_cloud_api"
        or incoming.channel_type != "whatsapp"
        or incoming.observation_kind != "message"
        or (
            query.expected_message_id is not None
            and incoming.message_id != query.expected_message_id
        )
    ):
        return None
    data = incoming.normalized_payload
    body = data.get("body")
    reference = reference_from_body(body) if isinstance(body, str) else None
    if reference is None:
        return None
    candidates = list(
        db.scalars(
            select(InboxProviderObservation)
            .join(InboxMessage, InboxProviderObservation.message_id == InboxMessage.id)
            .where(
                InboxProviderObservation.provider == "fiber_website",
                InboxProviderObservation.provider_account_scope == "fiber.dotmac.ng",
                InboxProviderObservation.channel_type == "website_fiber",
                InboxProviderObservation.processing_status == "processed",
                InboxMessage.metadata_["reference"].as_string() == reference,
            )
            .limit(2)
        ).all()
    )
    if len(candidates) != 1:
        return None
    website = candidates[0]
    original = website.normalized_payload
    sender, phone = data.get("contact_address"), original.get("phone")
    if not isinstance(sender, str) or not isinstance(phone, str):
        return None
    try:
        attribution = FiberAttribution.model_validate(
            {
                "journey_id": original.get("journey_id"),
                "utm_source": original.get("utm_source"),
                "utm_medium": original.get("utm_medium"),
                "utm_campaign": original.get("utm_campaign"),
                "utm_content": original.get("utm_content"),
                "utm_term": original.get("utm_term"),
                "campaign_id": original.get("external_campaign_id"),
                "ad_set_id": original.get("external_ad_set_id"),
                "ad_id": original.get("external_ad_id"),
                "click_id": original.get("external_click_id"),
                "landing_path": original.get("landing_path"),
                "captured_at": original.get("captured_at"),
            },
            strict=False,
        )
        return match_handoff(
            website=WebsiteHandoffFact(
                observation_id=website.id,
                integration_inbox_id=UUID(str(original["integration_inbox_id"])),
                provider=website.provider,
                site=website.provider_account_scope,
                form_version=str(original.get("form_version") or ""),
                delivery_id=str(website.external_message_id or ""),
                phone=phone,
                whatsapp_account_id=str(original.get("whatsapp_account_id") or ""),
                submitted_at=_aware(website.observed_at),
                attribution=attribution,
            ),
            whatsapp=WhatsAppHandoffFact(
                provider=incoming.provider,
                account_id=incoming.provider_account_scope,
                phone=sender,
                body=str(body),
                observed_at=_aware(incoming.observed_at),
            ),
            country_code=default_country_code(db),
        )
    except (KeyError, TypeError, ValueError):
        return None
