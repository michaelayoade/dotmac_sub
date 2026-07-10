from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
)
from app.services import email as email_service
from app.services import team_inbox_routing, team_outbound
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.integrations.connectors import whatsapp as whatsapp_connector

_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class InboxReplyPayload:
    body_html: str
    body_text: str | None = None
    subject: str | None = None
    to_email: str | None = None
    sent_by_person_id: str | UUID | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class InboxReplyResult:
    kind: str
    conversation_id: str
    message_id: str | None = None
    service_team_id: str | None = None
    sender_key: str | None = None
    activity: str | None = None
    from_address: str | None = None
    to_email: str | None = None
    reason: str | None = None


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _owner_team_link(conversation: InboxConversation) -> InboxConversationTeam | None:
    for link in conversation.team_links:
        if link.is_active and link.role == InboxTeamRole.owner.value:
            return link
    return None


def _owner_team_id(conversation: InboxConversation) -> UUID | None:
    if conversation.primary_service_team_id is not None:
        return conversation.primary_service_team_id
    link = _owner_team_link(conversation)
    return link.service_team_id if link is not None else None


def _reply_subject(conversation: InboxConversation, explicit: str | None) -> str:
    raw = (explicit or conversation.subject or "Message").strip() or "Message"
    if raw.lower().startswith("re:"):
        return raw[:200]
    return f"Re: {raw}"[:200]


def _reply_to_address(
    conversation: InboxConversation, explicit: str | None
) -> str | None:
    return team_inbox_routing.normalize_email_address(
        explicit or conversation.contact_address
    )


def _plain_text_reply(payload: InboxReplyPayload) -> str:
    body_text = (payload.body_text or "").strip()
    if body_text:
        return body_text
    body_html = (payload.body_html or "").strip()
    if not body_html:
        return ""
    text = _HTML_TAG_RE.sub(" ", body_html)
    return html.unescape(" ".join(text.split())).strip()


def _provider_metadata(result: dict[str, Any]) -> dict[str, Any]:
    provider_message_id = _provider_message_id(result)
    metadata = {
        "provider": result.get("provider"),
        "sent": result.get("sent"),
        "status_code": result.get("status_code"),
        "message": result.get("message"),
        "provider_message_id": provider_message_id,
    }
    response = result.get("response")
    if response is not None:
        metadata["response"] = str(response)[:500]
    return {key: value for key, value in metadata.items() if value is not None}


def _provider_message_id(result: dict[str, Any]) -> str | None:
    for key in ("provider_message_id", "message_id", "id"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    raw_response = result.get("response")
    if isinstance(raw_response, str):
        try:
            response = json.loads(raw_response)
        except json.JSONDecodeError:
            return None
    else:
        response = raw_response
    if not isinstance(response, dict):
        return None
    messages = response.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            value = str(first.get("id") or "").strip()
            return value or None
    return None


def _send_whatsapp_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    now: datetime | None,
) -> InboxReplyResult:
    recipient = normalize_phone_identifier(conversation.contact_address)
    if not recipient:
        return InboxReplyResult(
            kind="missing_recipient",
            conversation_id=str(conversation.id),
            reason="Conversation has no WhatsApp reply recipient",
        )
    body_text = _plain_text_reply(payload)
    if not body_text:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="Reply body is required",
        )

    result = whatsapp_connector.send_text_message(
        db,
        recipient=recipient,
        body=body_text,
    )
    if not bool(result.get("ok")):
        return InboxReplyResult(
            kind="send_failed",
            conversation_id=str(conversation.id),
            to_email=recipient,
            reason="WhatsApp provider rejected the reply",
        )

    sent_at = now or datetime.now(UTC)
    metadata = dict(payload.metadata or {})
    provider_message_id = _provider_message_id(result)
    metadata.update(
        {
            "source": "team_inbox_reply",
            "channel_type": InboxChannelType.whatsapp.value,
            "sent_by_person_id": str(payload.sent_by_person_id)
            if payload.sent_by_person_id
            else None,
            "provider_result": _provider_metadata(result),
        }
    )
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.whatsapp.value,
        direction=InboxMessageDirection.outbound.value,
        subject=None,
        body=body_text,
        external_message_id=provider_message_id,
        external_thread_id=conversation.external_thread_id,
        from_address=None,
        to_addresses=[recipient],
        cc_addresses=[],
        sent_at=sent_at,
        metadata_=metadata,
    )
    db.add(message)
    conversation.last_message_at = sent_at
    db.flush()
    return InboxReplyResult(
        kind="sent",
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        to_email=recipient,
    )


def send_inbox_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    now: datetime | None = None,
) -> InboxReplyResult:
    if not conversation.is_active:
        return InboxReplyResult(
            kind="invalid_conversation",
            conversation_id=str(conversation.id),
            reason="Conversation is inactive",
        )
    if conversation.status == InboxConversationStatus.resolved.value:
        return InboxReplyResult(
            kind="invalid_conversation",
            conversation_id=str(conversation.id),
            reason="Resolved conversations cannot be replied to",
        )

    if conversation.channel_type == InboxChannelType.whatsapp.value:
        return _send_whatsapp_reply(
            db,
            conversation=conversation,
            payload=payload,
            now=now,
        )

    to_email = _reply_to_address(conversation, payload.to_email)
    if not to_email:
        return InboxReplyResult(
            kind="missing_recipient",
            conversation_id=str(conversation.id),
            reason="Conversation has no reply recipient",
        )

    body_html = (payload.body_html or "").strip()
    if not body_html:
        return InboxReplyResult(
            kind="empty_body",
            conversation_id=str(conversation.id),
            reason="Reply body is required",
        )

    owner_link = _owner_team_link(conversation)
    service_team_id = _owner_team_id(conversation)
    if owner_link is None and service_team_id is None:
        owner_link = (
            db.query(InboxConversationTeam)
            .filter(InboxConversationTeam.conversation_id == conversation.id)
            .filter(InboxConversationTeam.role == InboxTeamRole.owner.value)
            .filter(InboxConversationTeam.is_active.is_(True))
            .one_or_none()
        )
        service_team_id = owner_link.service_team_id if owner_link is not None else None
    sender = team_outbound.resolve_team_email_sender(
        db,
        service_team_id=service_team_id,
        fallback_activity="support_ticket",
        metadata_override=owner_link.metadata_ if owner_link is not None else None,
    )
    config = sender.config
    subject = _reply_subject(conversation, payload.subject)
    sent = email_service.send_email(
        db,
        to_email=to_email,
        subject=subject,
        body_html=body_html,
        body_text=payload.body_text,
        sender_key=sender.sender_key,
        activity=sender.activity,
    )
    if not sent:
        return InboxReplyResult(
            kind="send_failed",
            conversation_id=str(conversation.id),
            service_team_id=sender.service_team_id,
            sender_key=config.get("sender_key") or sender.sender_key,
            activity=sender.activity,
            from_address=config.get("from_email"),
            to_email=to_email,
            reason="Email provider rejected the reply",
        )

    sent_at = now or datetime.now(UTC)
    metadata = dict(payload.metadata or {})
    metadata.update(
        {
            "source": "team_inbox_reply",
            "service_team_id": sender.service_team_id,
            "sender_key": config.get("sender_key") or sender.sender_key,
            "activity": sender.activity,
            "sent_by_person_id": str(payload.sent_by_person_id)
            if payload.sent_by_person_id
            else None,
        }
    )
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.email.value,
        direction=InboxMessageDirection.outbound.value,
        subject=subject,
        body=body_html,
        external_thread_id=conversation.external_thread_id,
        from_address=config.get("from_email"),
        to_addresses=[to_email],
        cc_addresses=[],
        sent_at=sent_at,
        metadata_=metadata,
    )
    db.add(message)
    conversation.last_message_at = sent_at
    db.flush()
    return InboxReplyResult(
        kind="sent",
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        service_team_id=sender.service_team_id,
        sender_key=config.get("sender_key") or sender.sender_key,
        activity=sender.activity,
        from_address=config.get("from_email"),
        to_email=to_email,
    )
