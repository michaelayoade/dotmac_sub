from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
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
from app.services import team_inbox_realtime, team_inbox_routing, team_outbound
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.integrations.connectors import whatsapp as whatsapp_connector

_HTML_TAG_RE = re.compile(r"<[^>]+>")
T = TypeVar("T")


def _commit(db: Session, action: Callable[[], T]) -> T:
    try:
        result = action()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


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


def apply_whatsapp_delivery_status(
    db: Session,
    status_item: dict[str, Any],
) -> dict[str, object]:
    provider_message_id = str(status_item["message_id"])
    message = (
        db.query(InboxMessage)
        .filter(InboxMessage.channel_type == InboxChannelType.whatsapp.value)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(InboxMessage.external_message_id == provider_message_id)
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    if message is None:
        return {
            "kind": "not_found",
            "provider_message_id": provider_message_id,
            "status": status_item["status"],
        }

    metadata = dict(message.metadata_ or {})
    history = metadata.get("delivery_status_history")
    if not isinstance(history, list):
        history = []
    event = {
        "status": status_item["status"],
        "timestamp": status_item.get("timestamp"),
        "recipient_id": status_item.get("recipient_id"),
        "errors": status_item.get("errors"),
    }
    history.append({key: value for key, value in event.items() if value is not None})
    metadata["delivery_status"] = status_item["status"]
    metadata["delivery_status_at"] = status_item.get("timestamp")
    metadata["delivery_recipient_id"] = status_item.get("recipient_id")
    if status_item.get("errors") is not None:
        metadata["delivery_errors"] = status_item["errors"]
    metadata["delivery_status_history"] = history[-20:]
    message.metadata_ = metadata
    return {
        "kind": "updated",
        "message_id": str(message.id),
        "provider_message_id": provider_message_id,
        "status": status_item["status"],
    }


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
    record_failure: bool = False,
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

    payload_metadata = dict(payload.metadata or {})
    raw_template_spec = payload_metadata.get("whatsapp_template")
    template_spec = raw_template_spec if isinstance(raw_template_spec, dict) else None
    template_name = (
        str(template_spec.get("name") or "").strip() if template_spec else ""
    )
    use_template = bool(template_spec and template_name)
    if use_template:
        raw_variables = template_spec.get("variables") if template_spec else None
        result = whatsapp_connector.send_template_message(
            db,
            recipient=recipient,
            template_name=template_name,
            language=str(template_spec.get("language") or "").strip() or None
            if template_spec
            else None,
            variables=raw_variables if isinstance(raw_variables, dict) else None,
            dry_run=False,
        )
    else:
        result = whatsapp_connector.send_text_message(
            db,
            recipient=recipient,
            body=body_text,
        )
    if not bool(result.get("ok")):
        failed_message_id = None
        if record_failure:
            failed_message_id = _record_failed_outbound(
                db,
                conversation=conversation,
                payload=payload,
                channel_type=InboxChannelType.whatsapp.value,
                to_addresses=[recipient],
                reason="WhatsApp provider rejected the template"
                if use_template
                else "WhatsApp provider rejected the reply",
                provider_result=_provider_metadata(result),
                now=now,
            )
        return InboxReplyResult(
            kind="send_failed",
            conversation_id=str(conversation.id),
            message_id=failed_message_id,
            to_email=recipient,
            reason="WhatsApp provider rejected the template"
            if use_template
            else "WhatsApp provider rejected the reply",
        )

    sent_at = now or datetime.now(UTC)
    metadata = payload_metadata
    provider_message_id = _provider_message_id(result)
    metadata.update(
        {
            "source": "team_inbox_reply",
            "channel_type": InboxChannelType.whatsapp.value,
            "message_kind": "template" if use_template else "text",
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
        body=body_text if not use_template else f"[WhatsApp template: {template_name}]",
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
    team_inbox_realtime.publish_conversation_event(
        str(conversation.id),
        event_type=team_inbox_realtime.EventType.MESSAGE_NEW,
        payload=team_inbox_realtime.message_event_payload(
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            body=message.body,
            direction=message.direction,
            channel_type=message.channel_type,
            created_at=message.created_at,
            author_name="Support",
            extra={"sender_type": "agent", "from_customer": False},
        ),
    )
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
    record_failure: bool = False,
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
            record_failure=record_failure,
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
        failed_message_id = None
        if record_failure:
            failed_message_id = _record_failed_outbound(
                db,
                conversation=conversation,
                payload=payload,
                channel_type=InboxChannelType.email.value,
                to_addresses=[to_email],
                from_address=config.get("from_email"),
                subject=subject,
                reason="Email provider rejected the reply",
                metadata={
                    "service_team_id": sender.service_team_id,
                    "sender_key": config.get("sender_key") or sender.sender_key,
                    "activity": sender.activity,
                },
                now=now,
            )
        return InboxReplyResult(
            kind="send_failed",
            conversation_id=str(conversation.id),
            message_id=failed_message_id,
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
    team_inbox_realtime.publish_conversation_event(
        str(conversation.id),
        event_type=team_inbox_realtime.EventType.MESSAGE_NEW,
        payload=team_inbox_realtime.message_event_payload(
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            body=message.body,
            direction=message.direction,
            channel_type=message.channel_type,
            created_at=message.created_at,
            author_name="Support",
            extra={"sender_type": "agent", "from_customer": False},
        ),
    )
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


def send_inbox_reply_for_conversation(
    db: Session,
    *,
    conversation_id: str | UUID,
    payload: InboxReplyPayload,
    now: datetime | None = None,
    record_failure: bool = False,
) -> InboxReplyResult:
    conversation_uuid = _coerce_uuid(conversation_id)
    conversation = (
        db.get(InboxConversation, conversation_uuid) if conversation_uuid else None
    )
    if conversation is None:
        return InboxReplyResult(
            kind="conversation_not_found",
            conversation_id=str(conversation_id),
            reason="Conversation not found",
        )
    return send_inbox_reply(
        db,
        conversation=conversation,
        payload=payload,
        now=now,
        record_failure=record_failure,
    )


def send_inbox_reply_for_conversation_committed(
    db: Session,
    *,
    conversation_id: str | UUID,
    payload: InboxReplyPayload,
    now: datetime | None = None,
    record_failure: bool = False,
) -> InboxReplyResult:
    return _commit(
        db,
        lambda: send_inbox_reply_for_conversation(
            db,
            conversation_id=conversation_id,
            payload=payload,
            now=now,
            record_failure=record_failure,
        ),
    )


def _record_failed_outbound(
    db: Session,
    *,
    conversation: InboxConversation,
    payload: InboxReplyPayload,
    channel_type: str,
    to_addresses: list[str],
    reason: str,
    provider_result: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    from_address: str | None = None,
    subject: str | None = None,
    now: datetime | None = None,
) -> str:
    attempted_at = now or datetime.now(UTC)
    combined_metadata = dict(payload.metadata or {})
    combined_metadata.update(metadata or {})
    combined_metadata.update(
        {
            "source": "team_inbox_reply",
            "delivery_status": "failed",
            "send_error": reason,
            "retry_count": 0,
            "sent_by_person_id": str(payload.sent_by_person_id)
            if payload.sent_by_person_id
            else None,
        }
    )
    if provider_result:
        combined_metadata["provider_result"] = provider_result
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=channel_type,
        direction=InboxMessageDirection.outbound.value,
        subject=subject or payload.subject,
        body=payload.body_text or payload.body_html,
        external_thread_id=conversation.external_thread_id,
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=[],
        sent_at=attempted_at,
        metadata_=combined_metadata,
    )
    db.add(message)
    conversation.last_message_at = attempted_at
    db.flush()
    return str(message.id)


def retry_outbound_message(
    db: Session,
    *,
    message: InboxMessage,
    sent_by_person_id: str | UUID | None = None,
    now: datetime | None = None,
) -> InboxReplyResult:
    metadata = dict(message.metadata_ or {})
    if metadata.get("delivery_status") != "failed":
        return InboxReplyResult(
            kind="invalid_message",
            conversation_id=str(message.conversation_id),
            message_id=str(message.id),
            reason="Only failed outbound inbox messages can be retried",
        )
    conversation = db.get(InboxConversation, message.conversation_id)
    if conversation is None:
        return InboxReplyResult(
            kind="invalid_conversation",
            conversation_id=str(message.conversation_id),
            message_id=str(message.id),
            reason="Conversation not found",
        )
    retry_count = int(metadata.get("retry_count") or 0) + 1
    result = send_inbox_reply(
        db,
        conversation=conversation,
        payload=InboxReplyPayload(
            body_html=message.body or "",
            body_text=message.body,
            subject=message.subject,
            to_email=(message.to_addresses or [None])[0],
            sent_by_person_id=sent_by_person_id,
            metadata={
                "source_route": "team_inbox_retry",
                "retry_of_message_id": str(message.id),
                "retry_count": retry_count,
            },
        ),
        now=now,
        record_failure=False,
    )
    metadata["retry_count"] = retry_count
    metadata["last_retry_at"] = (now or datetime.now(UTC)).isoformat()
    metadata["last_retry_result"] = result.kind
    if result.kind == "sent":
        metadata["delivery_status"] = "retried"
        metadata["retried_message_id"] = result.message_id
    message.metadata_ = metadata
    db.flush()
    return result
