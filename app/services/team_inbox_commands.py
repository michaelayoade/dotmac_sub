"""Committed command boundary for team-inbox UI adapters.

The underlying team-inbox services own their focused policies. This module owns
admin command orchestration, model lookup, and the transaction boundary so web
routes never become a parallel writer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.party import (
    Party,
    PartyContactPointType,
    PartyRelationship,
    PartyRelationshipType,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
)
from app.models.sales import Lead
from app.models.subscriber import Reseller, Subscriber
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxSavedFilter,
)
from app.schemas.sales import (
    LeadCapturePartyCreate,
    LeadCaptureRequest,
    LeadContactObservation,
    LeadOriginCaptureCreate,
)
from app.services import (
    party as party_service,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_contact_links,
    team_inbox_media,
    team_inbox_operations,
    team_inbox_outbound,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales import account_conversion
from app.services.sales import capture as sales_capture

T = TypeVar("T")


OWNER = "communications.team_inbox_commands"
_ADMIN_MUTATION = OwnerCommandDefinition(
    owner=OWNER,
    concern="operator conversation and collaboration commands",
    name="execute_team_inbox_admin_mutation",
)


class InboxCommandError(DomainError, ValueError):
    """Base error safe for an admin adapter to render."""

    def __init__(
        self,
        message: str,
        *,
        suffix: str = "rejected",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code=f"{OWNER}.{suffix}", message=message, details=details)


class ConversationNotFoundError(InboxCommandError):
    def __init__(self, message: str = "Conversation not found.") -> None:
        super().__init__(message, suffix="conversation_not_found")


class MessageNotFoundError(InboxCommandError):
    def __init__(self, message: str = "Message not found.") -> None:
        super().__init__(message, suffix="message_not_found")


class InboxCommandRejected(InboxCommandError):
    def __init__(self, message: str, *, conversation_id: UUID | str | None = None):
        super().__init__(
            message,
            suffix="command_rejected",
            details={"conversation_id": str(conversation_id)}
            if conversation_id
            else None,
        )
        self.conversation_id = str(conversation_id) if conversation_id else None


@dataclass(frozen=True)
class ReplyOutcome:
    conversation_id: str
    kind: str
    sender: str
    replayed: bool = False


@dataclass(frozen=True)
class AttachmentUploadOutcome:
    conversation_id: str
    attachment_ids: tuple[str, ...]


@dataclass(frozen=True)
class ContactLinkOutcome:
    conversation_id: str
    channel_type: str
    target: str


@dataclass(frozen=True)
class LeadCreationOutcome:
    conversation_id: str
    lead_id: str
    party_id: str
    replayed: bool


@dataclass(frozen=True)
class LeadMergeOutcome:
    conversation_id: str
    lead_id: str
    target_type: str
    target_id: str
    subscriber_id: str | None = None
    reseller_id: str | None = None
    organization_id: str | None = None


@dataclass(frozen=True)
class ContactMergeOutcome:
    conversation_id: str
    target_type: str
    target_id: str
    lead_id: str | None = None


@dataclass(frozen=True)
class BulkActionOutcome:
    message: str


@dataclass(frozen=True)
class StatusOutcome:
    conversation_id: str
    status: str
    already_set: bool


@dataclass(frozen=True)
class AgentPresenceOutcome:
    person_id: str
    status: str
    already_set: bool


def _commit(
    db: Session,
    action: Callable[[], T],
    *,
    context: CommandContext | None = None,
) -> T:
    command_context = context or CommandContext.system(
        actor="system:team-inbox-admin-adapter",
        scope="team-inbox:operator-command",
        reason="execute typed Team Inbox operator command",
    )
    return execute_owner_command(
        db,
        definition=_ADMIN_MUTATION,
        context=command_context,
        operation=action,
    )


def _active_conversation(
    db: Session,
    conversation_id: str | UUID,
    *,
    for_update: bool = False,
) -> InboxConversation:
    conversation_uuid = coerce_uuid(conversation_id)
    conversation = None
    if conversation_uuid is not None:
        query = db.query(InboxConversation).filter(
            InboxConversation.id == conversation_uuid
        )
        if for_update:
            query = query.with_for_update()
        conversation = query.one_or_none()
    if conversation is None or not conversation.is_active:
        raise ConversationNotFoundError("Conversation not found.")
    return conversation


def reply(
    db: Session,
    *,
    conversation_id: str | UUID,
    body_text: str,
    actor_person_id: str | UUID | None,
    macro_id: str | UUID | None = None,
    template_id: str | UUID | None = None,
    idempotency_key: str | None = None,
    reply_to_message_id: str | UUID | None = None,
    attachment_ids: Sequence[str | UUID] = (),
) -> ReplyOutcome:
    def action() -> ReplyOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        clean_body = str(body_text or "").strip()
        clean_idempotency_key = str(idempotency_key or "").strip()
        reply_to_uuid = coerce_uuid(reply_to_message_id)
        clean_attachment_ids: list[UUID] = []
        for raw_attachment_id in attachment_ids:
            attachment_uuid = coerce_uuid(raw_attachment_id)
            if attachment_uuid is None:
                raise InboxCommandRejected(
                    "Attachment reference is invalid.",
                    conversation_id=conversation.id,
                )
            clean_attachment_ids.append(attachment_uuid)
        clean_attachment_ids = list(dict.fromkeys(clean_attachment_ids))
        if reply_to_message_id and reply_to_uuid is None:
            raise InboxCommandRejected(
                "Quoted message is invalid.",
                conversation_id=conversation.id,
            )
        if len(clean_idempotency_key) > 200:
            raise InboxCommandError("Reply idempotency key is too long.")
        if clean_idempotency_key:
            previous = (
                db.query(InboxMessage)
                .filter(InboxMessage.conversation_id == conversation.id)
                .filter(InboxMessage.direction == "outbound")
                .filter(
                    InboxMessage.metadata_["idempotency_key"].as_string()
                    == clean_idempotency_key
                )
                .order_by(InboxMessage.created_at.desc())
                .first()
            )
            if previous is not None:
                previous_body = str(
                    (previous.metadata_ or {}).get("body_text") or ""
                ).strip()
                previous_reply = (previous.metadata_ or {}).get("reply_to")
                previous_reply_id = (
                    str(previous_reply.get("message_id") or "")
                    if isinstance(previous_reply, dict)
                    else ""
                )
                previous_attachment_ids = tuple(
                    str(item)
                    for item in (
                        (previous.metadata_ or {}).get("inbox_attachment_ids") or ()
                    )
                )
                requested_reply_id = str(reply_to_uuid) if reply_to_uuid else ""
                if (
                    previous_body
                    and previous_body != clean_body
                    or previous_reply_id != requested_reply_id
                    or previous_attachment_ids
                    != tuple(str(item) for item in clean_attachment_ids)
                ):
                    raise InboxCommandRejected(
                        "This send key was already used for a different reply.",
                        conversation_id=conversation.id,
                    )
                return ReplyOutcome(
                    conversation_id=str(conversation.id),
                    kind=str(
                        (previous.metadata_ or {}).get("delivery_status") or "queued"
                    ),
                    sender=previous.from_address or "team sender",
                    replayed=True,
                )
        template = None
        clean_template_id = (
            str(template_id).strip()
            if isinstance(template_id, (str, UUID)) and str(template_id).strip()
            else None
        )
        if clean_template_id:
            template = team_inbox_operations.get_template(db, clean_template_id)
            if not clean_body:
                clean_body = template.body_text.strip()
        if not clean_body and not clean_attachment_ids:
            raise InboxCommandError("Reply body is required.")

        body_html = (
            "<p>"
            + "<br>".join(escape(line) for line in clean_body.splitlines())
            + "</p>"
        )
        reply_metadata: dict[str, object] = {
            "source_route": "admin_inbox_detail_reply",
            "template_id": str(template.id) if template is not None else None,
            "idempotency_key": clean_idempotency_key or None,
            "inbox_attachment_ids": [str(item) for item in clean_attachment_ids],
        }
        if reply_to_uuid is not None:
            quoted_message = db.get(InboxMessage, reply_to_uuid)
            if (
                quoted_message is None
                or quoted_message.conversation_id != conversation.id
            ):
                raise InboxCommandRejected(
                    "Quoted message does not belong to this conversation.",
                    conversation_id=conversation.id,
                )
            reply_metadata["reply_to"] = {
                "message_id": str(quoted_message.id),
                "author": quoted_message.from_address
                or (
                    "Support agent"
                    if quoted_message.direction == "outbound"
                    else "Customer"
                ),
                "excerpt": str(quoted_message.body or "")[:240],
            }
        if (
            template is not None
            and conversation.channel_type == InboxChannelType.whatsapp.value
        ):
            template_metadata = dict(template.metadata_ or {})
            provider_template_name = str(
                template_metadata.get("provider_template_name")
                or template_metadata.get("whatsapp_template_name")
                or ""
            ).strip()
            if provider_template_name:
                variables = template_metadata.get("provider_template_variables")
                reply_metadata["whatsapp_template"] = {
                    "name": provider_template_name,
                    "language": str(
                        template_metadata.get("provider_template_language") or ""
                    ).strip()
                    or None,
                    "variables": variables if isinstance(variables, dict) else {},
                    "inbox_template_id": str(template.id),
                }

        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html=body_html,
                body_text=clean_body,
                subject=template.subject if template is not None else None,
                sent_by_person_id=actor_person_id,
                metadata=reply_metadata,
            ),
            record_failure=True,
        )
        if result.kind not in {"sent", "queued"}:
            raise InboxCommandRejected(
                result.reason or "Reply could not be sent.",
                conversation_id=conversation.id,
            )
        if clean_attachment_ids and result.message_id:
            message_uuid = coerce_uuid(result.message_id)
            if message_uuid is None:
                raise InboxCommandRejected(
                    "Reply was created without a valid message reference.",
                    conversation_id=conversation.id,
                )
            team_inbox_media.bind_assets_to_message(
                db,
                conversation_id=conversation.id,
                message_id=message_uuid,
                asset_ids=clean_attachment_ids,
            )
        team_inbox_operations.record_macro_use(db, macro_id)
        return ReplyOutcome(
            conversation_id=str(conversation.id),
            kind=result.kind,
            sender=result.from_address or result.sender_key or "team sender",
        )

    return _commit(db, action)


def create_label(db: Session, *, name: str, color: str | None = None) -> None:
    _commit(
        db,
        lambda: team_inbox_operations.create_or_reactivate_label(
            db, name=name, color=color
        ),
    )


def apply_label(
    db: Session,
    *,
    conversation_id: str | UUID,
    label_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        conversation = _active_conversation(db, conversation_id)
        team_inbox_operations.apply_label(
            db,
            conversation=conversation,
            label_id=label_id,
            applied_by_person_id=actor_person_id,
        )

    _commit(db, action)


def remove_label(
    db: Session,
    *,
    conversation_id: str | UUID,
    label_id: str | UUID,
) -> None:
    def action() -> None:
        team_inbox_operations.remove_label(
            db,
            conversation=_active_conversation(db, conversation_id),
            label_id=label_id,
        )

    _commit(db, action)


def create_macro(
    db: Session,
    *,
    name: str,
    body_text: str,
    description: str | None = None,
    visibility: str = "shared",
    actor_person_id: str | UUID | None = None,
) -> None:
    _commit(
        db,
        lambda: team_inbox_operations.create_macro(
            db,
            name=name,
            body_text=body_text,
            description=description,
            visibility=visibility,
            created_by_person_id=actor_person_id,
        ),
    )


def create_template(
    db: Session,
    *,
    name: str,
    channel_type: str,
    subject: str | None,
    body_text: str,
    provider_template_name: str | None = None,
    provider_template_language: str | None = None,
) -> None:
    metadata = {
        key: value
        for key, value in {
            "provider_template_name": str(provider_template_name or "").strip(),
            "provider_template_language": str(provider_template_language or "").strip(),
        }.items()
        if value
    }
    _commit(
        db,
        lambda: team_inbox_operations.create_template(
            db,
            name=name,
            channel_type=channel_type,
            subject=subject,
            body_text=body_text,
            metadata=metadata or None,
        ),
    )


def retry_message(
    db: Session,
    *,
    message_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> str:
    def action() -> str:
        message_uuid = coerce_uuid(message_id)
        message = db.get(InboxMessage, message_uuid) if message_uuid else None
        if message is None:
            raise MessageNotFoundError("Message not found.")
        result = team_inbox_outbound.retry_outbound_message(
            db,
            message=message,
            sent_by_person_id=actor_person_id,
        )
        if result.kind not in {"sent", "queued"}:
            raise InboxCommandRejected(
                result.reason or "Retry failed.",
                conversation_id=message.conversation_id,
            )
        return str(message.conversation_id)

    return _commit(db, action)


def retry_failed_batch(db: Session, *, limit: int = 50) -> int:
    def action() -> int:
        result = team_inbox_operations.retry_failed_outbound_batch(db, limit=limit)
        retried = result.get("retried")
        return len(retried) if isinstance(retried, list) else 0

    return _commit(db, action)


def update_workflow(
    db: Session,
    *,
    conversation_id: str | UUID,
    priority: int | None = None,
    is_muted: bool | None = None,
    snooze_minutes: int | None = None,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        team_inbox_operations.update_conversation_workflow(
            db,
            conversation=_active_conversation(db, conversation_id),
            priority=priority,
            is_muted=is_muted,
            snooze_minutes=snooze_minutes,
            actor_person_id=actor_person_id,
        )

    _commit(db, action)


def save_filter(
    db: Session,
    *,
    name: str,
    filter_payload: dict[str, Any],
    actor_person_id: str | UUID | None = None,
    is_shared: bool = False,
) -> None:
    _commit(
        db,
        lambda: team_inbox_operations.save_filter(
            db,
            name=name,
            filter_payload=filter_payload,
            owner_person_id=actor_person_id,
            is_shared=is_shared,
        ),
    )


def delete_filter(
    db: Session,
    *,
    filter_id: str | UUID,
    actor_person_id: str | UUID | None,
) -> None:
    def action() -> None:
        filter_uuid = coerce_uuid(filter_id)
        actor_uuid = coerce_uuid(actor_person_id)
        saved_filter = db.get(InboxSavedFilter, filter_uuid) if filter_uuid else None
        if saved_filter is None or not saved_filter.is_active:
            raise InboxCommandError("Saved filter not found.")
        if actor_uuid is None or saved_filter.owner_person_id != actor_uuid:
            raise InboxCommandRejected("Only the saved view owner can delete it.")
        team_inbox_operations.delete_saved_filter(db, filter_id=saved_filter.id)

    _commit(db, action)


def set_agent_presence(
    db: Session,
    *,
    actor_person_id: str | UUID | None,
    status: str,
) -> AgentPresenceOutcome:
    actor_uuid = coerce_uuid(actor_person_id)
    if actor_uuid is None:
        raise InboxCommandRejected("Authenticated operator identity is required.")
    clean_status = str(status or "").strip().lower()
    if clean_status not in team_inbox_assignment.VALID_AGENT_PRESENCE_STATUSES:
        raise InboxCommandError("Unsupported inbox availability status.")

    def action() -> AgentPresenceOutcome:
        existing = (
            db.query(InboxAgentPresence)
            .filter(InboxAgentPresence.person_id == actor_uuid)
            .one_or_none()
        )
        previous = (
            existing.manual_override_status or existing.status
            if existing is not None
            else None
        )
        presence = team_inbox_assignment.set_agent_presence(
            db,
            person_id=actor_uuid,
            status=clean_status,
        )
        return AgentPresenceOutcome(
            person_id=str(actor_uuid),
            status=presence.manual_override_status or presence.status,
            already_set=previous == clean_status,
        )

    return _commit(db, action)


def bulk_action(
    db: Session,
    *,
    conversation_ids: Sequence[str | UUID],
    action: str,
    status_value: str | None = None,
    priority: int | None = None,
    label_id: str | UUID | None = None,
    service_team_id: str | UUID | None = None,
    assigned_person_id: str | UUID | None = None,
    auto_assign: bool = True,
    actor_person_id: str | UUID | None = None,
) -> BulkActionOutcome:
    if not conversation_ids:
        raise InboxCommandError("Select at least one conversation.")

    def execute() -> BulkActionOutcome:
        if action == "status":
            result = team_inbox_operations.bulk_update_status(
                db,
                conversation_ids=conversation_ids,
                status_value=status_value or "",
                actor_person_id=actor_person_id,
            )
            verb = "Updated"
            noun = "conversation statuses"
        elif action == "priority":
            result = team_inbox_operations.bulk_update_priority(
                db,
                conversation_ids=conversation_ids,
                priority=priority,
                actor_person_id=actor_person_id,
            )
            verb = "Updated priority for"
            noun = "conversations"
        elif action == "label":
            result = team_inbox_operations.bulk_apply_label(
                db,
                conversation_ids=conversation_ids,
                label_id=label_id or "",
                actor_person_id=actor_person_id,
            )
            verb = "Applied label to"
            noun = "conversations"
        elif action == "escalate":
            result = team_inbox_operations.bulk_escalate(
                db,
                conversation_ids=conversation_ids,
                service_team_id=service_team_id or "",
                assigned_person_id=assigned_person_id,
                auto_assign=auto_assign,
                actor_person_id=actor_person_id,
                reason="Bulk inbox escalation",
            )
            verb = "Escalated"
            noun = "conversations"
        else:
            raise InboxCommandError("Unsupported bulk action.")
        updated = result.get("updated")
        count = len(updated) if isinstance(updated, list) else 0
        return BulkActionOutcome(message=f"{verb} {count} {noun}.")

    return _commit(db, execute)


def link_contact(
    db: Session,
    *,
    conversation_id: str | UUID,
    target_type: str,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    subscriber_id_manual: str | UUID | None = None,
    reseller_id_manual: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
    note: str | None = None,
) -> ContactLinkOutcome:
    def action() -> ContactLinkOutcome:
        conversation = _active_conversation(db, conversation_id)
        selected_subscriber = (
            str(subscriber_id_manual or subscriber_id or "").strip() or None
        )
        selected_reseller = str(reseller_id_manual or reseller_id or "").strip() or None
        if target_type == "subscriber":
            selected_reseller = None
        elif target_type == "reseller":
            selected_subscriber = None
        else:
            raise InboxCommandError(
                "Choose whether this contact belongs to a subscriber or reseller."
            )
        result = team_inbox_contact_links.link_conversation_contact(
            db,
            conversation=conversation,
            subscriber_id=selected_subscriber,
            reseller_id=selected_reseller,
            linked_by_person_id=actor_person_id,
            note=note,
        )
        return ContactLinkOutcome(
            conversation_id=str(conversation.id),
            channel_type=conversation.channel_type,
            target="subscriber" if result.subscriber_id else "reseller",
        )

    return _commit(db, action)


def stage_attachments(
    db: Session,
    *,
    conversation_id: str | UUID,
    files: Sequence[team_inbox_media.StagedAttachmentInput],
) -> AttachmentUploadOutcome:
    def action() -> AttachmentUploadOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        if not files:
            raise InboxCommandRejected(
                "Choose at least one attachment.",
                conversation_id=conversation.id,
            )
        assets = team_inbox_media.stage_outbound_attachments(
            db,
            conversation_id=conversation.id,
            files=files,
        )
        return AttachmentUploadOutcome(
            conversation_id=str(conversation.id),
            attachment_ids=tuple(str(asset.id) for asset in assets),
        )

    return _commit(db, action)


def _lead_source_for_channel(channel_type: str) -> str:
    return {
        InboxChannelType.email.value: "Email",
        InboxChannelType.whatsapp.value: "Whatsapp",
        InboxChannelType.facebook_messenger.value: "Facebook",
        InboxChannelType.instagram_dm.value: "Instagram",
        InboxChannelType.chat_widget.value: "Website",
    }.get(channel_type, "Website")


def _party_contact_type_for_channel(channel_type: str) -> PartyContactPointType | None:
    values = {
        InboxChannelType.email.value: PartyContactPointType.email,
        InboxChannelType.whatsapp.value: PartyContactPointType.whatsapp,
        InboxChannelType.facebook_messenger.value: PartyContactPointType.facebook_messenger,
        InboxChannelType.instagram_dm.value: PartyContactPointType.instagram_dm,
    }
    return values.get(channel_type)


def create_lead_from_conversation(
    db: Session,
    *,
    conversation_id: str | UUID,
    actor_person_id: str | UUID | None = None,
    title: str | None = None,
    note: str | None = None,
) -> LeadCreationOutcome:
    def action() -> LeadCreationOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        if conversation.subscriber_id is not None:
            raise InboxCommandRejected(
                "This conversation is already linked to a customer.",
                conversation_id=conversation.id,
            )
        if not conversation.contact_address:
            raise InboxCommandRejected(
                "Conversation has no contact address to capture as a lead.",
                conversation_id=conversation.id,
            )
        metadata = dict(conversation.metadata_ or {})
        existing = metadata.get("lead_capture")
        if (
            isinstance(existing, dict)
            and existing.get("lead_id")
            and existing.get("party_id")
        ):
            return LeadCreationOutcome(
                conversation_id=str(conversation.id),
                lead_id=str(existing["lead_id"]),
                party_id=str(existing["party_id"]),
                replayed=True,
            )

        channel_type = conversation.channel_type
        contact_type = _party_contact_type_for_channel(channel_type)
        contact_address = str(conversation.contact_address or "").strip()
        contacts: list[LeadContactObservation] = []
        if contact_type is not None:
            provider = None
            provider_account_id = None
            external_subject_id = None
            if channel_type in {
                InboxChannelType.facebook_messenger.value,
                InboxChannelType.instagram_dm.value,
            }:
                provider = str(metadata.get("provider") or "team_inbox")
                provider_account_id = str(
                    metadata.get("external_account_id")
                    or metadata.get("page_id")
                    or metadata.get("instagram_account_id")
                    or channel_type
                )
                external_subject_id = contact_address
            contacts.append(
                LeadContactObservation(
                    channel_type=contact_type,
                    value=contact_address,
                    display_value=contact_address,
                    provider=provider,
                    provider_account_id=provider_account_id,
                    external_subject_id=external_subject_id,
                    is_primary=True,
                )
            )

        clean_title = (
            str(title or "").strip()
            or conversation.subject
            or f"{channel_type.replace('_', ' ').title()} inbox enquiry"
        )
        payload = LeadCaptureRequest(
            party=LeadCapturePartyCreate(
                display_name=contact_address,
                contacts=contacts,
            ),
            title=clean_title[:200],
            lead_source=_lead_source_for_channel(channel_type),
            origin=LeadOriginCaptureCreate(
                capture_method="agent_declared",
                source_platform="agent",
                source_interaction_id=f"team-inbox:{conversation.id}",
                capture_source="admin_inbox",
                capture_reason="Operator created a lead from an unmatched inbox conversation",
            ),
            notes=str(note or "").strip() or None,
        )
        try:
            result = sales_capture.capture_lead(
                db,
                payload,
                actor_id=str(actor_person_id or "system:team-inbox-admin"),
                commit=False,
            )
        except sales_capture.LeadCaptureError as exc:
            raise InboxCommandRejected(
                str(exc), conversation_id=conversation.id
            ) from exc
        metadata["lead_capture"] = {
            "lead_id": str(result.lead.id),
            "party_id": str(result.party_id),
            "origin_capture_id": str(result.origin.id),
            "source": "admin_inbox",
            "actor_person_id": str(actor_person_id or "") or None,
            "replayed": result.replayed,
        }
        conversation.metadata_ = metadata
        db.flush()
        return LeadCreationOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(result.lead.id),
            party_id=str(result.party_id),
            replayed=result.replayed,
        )

    return _commit(db, action)


def _lead_from_conversation_metadata(
    db: Session, conversation: InboxConversation
) -> Lead:
    metadata = dict(conversation.metadata_ or {})
    lead_capture = metadata.get("lead_capture")
    lead_id = (
        coerce_uuid(lead_capture.get("lead_id"))
        if isinstance(lead_capture, dict)
        else None
    )
    lead = db.get(Lead, lead_id) if lead_id is not None else None
    if lead is None or lead.party_id is None:
        raise InboxCommandRejected(
            "Create a lead from this conversation before merging it.",
            conversation_id=conversation.id,
        )
    return lead


def _lead_from_conversation_or_create(
    db: Session,
    *,
    conversation: InboxConversation,
    actor_person_id: str | UUID | None,
) -> Lead:
    try:
        return _lead_from_conversation_metadata(db, conversation)
    except InboxCommandRejected:
        created = create_lead_from_conversation_uncommitted(
            db,
            conversation=conversation,
            actor_person_id=actor_person_id,
            title=None,
            note="Created during Inbox contact merge",
        )
        lead = db.get(Lead, UUID(created.lead_id))
        if lead is None:
            raise InboxCommandRejected("Created lead could not be reloaded.")
        return lead


def _unique_target(rows: list[T], *, target_name: str, query: str) -> T:
    if not rows:
        raise InboxCommandRejected(f"No {target_name} matched '{query}'.")
    if len(rows) > 1:
        raise InboxCommandRejected(
            f"More than one {target_name} matched '{query}'. Narrow the search."
        )
    return rows[0]


def _search_subscriber(db: Session, query_text: str) -> Subscriber:
    like = f"%{query_text}%"
    rows = (
        db.query(Subscriber)
        .filter(Subscriber.is_active.is_(True))
        .filter(
            or_(
                Subscriber.email.ilike(like),
                Subscriber.phone.ilike(like),
                Subscriber.first_name.ilike(like),
                Subscriber.last_name.ilike(like),
                Subscriber.display_name.ilike(like),
                Subscriber.company_name.ilike(like),
                Subscriber.account_number.ilike(like),
                Subscriber.subscriber_number.ilike(like),
            )
        )
        .order_by(Subscriber.updated_at.desc().nullslast())
        .limit(2)
        .all()
    )
    return _unique_target(rows, target_name="customer", query=query_text)


def _search_reseller(db: Session, query_text: str) -> Reseller:
    like = f"%{query_text}%"
    rows = (
        db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .filter(
            or_(
                Reseller.name.ilike(like),
                Reseller.code.ilike(like),
                Reseller.contact_email.ilike(like),
                Reseller.contact_phone.ilike(like),
            )
        )
        .order_by(Reseller.name.asc())
        .limit(2)
        .all()
    )
    return _unique_target(rows, target_name="reseller", query=query_text)


def _search_organization(db: Session, query_text: str) -> Organization:
    like = f"%{query_text}%"
    rows = (
        db.query(Organization)
        .filter(Organization.is_active.is_(True))
        .filter(
            or_(
                Organization.name.ilike(like),
                Organization.legal_name.ilike(like),
                Organization.domain.ilike(like),
                Organization.email.ilike(like),
                Organization.phone.ilike(like),
            )
        )
        .order_by(Organization.updated_at.desc().nullslast())
        .limit(2)
        .all()
    )
    return _unique_target(rows, target_name="business", query=query_text)


def create_lead_from_conversation_uncommitted(
    db: Session,
    *,
    conversation: InboxConversation,
    actor_person_id: str | UUID | None = None,
    title: str | None = None,
    note: str | None = None,
) -> LeadCreationOutcome:
    if conversation.subscriber_id is not None:
        raise InboxCommandRejected(
            "This conversation is already linked to a customer.",
            conversation_id=conversation.id,
        )
    if not conversation.contact_address:
        raise InboxCommandRejected(
            "Conversation has no contact address to capture as a lead.",
            conversation_id=conversation.id,
        )
    metadata = dict(conversation.metadata_ or {})
    existing = metadata.get("lead_capture")
    if (
        isinstance(existing, dict)
        and existing.get("lead_id")
        and existing.get("party_id")
    ):
        return LeadCreationOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(existing["lead_id"]),
            party_id=str(existing["party_id"]),
            replayed=True,
        )

    channel_type = conversation.channel_type
    contact_type = _party_contact_type_for_channel(channel_type)
    contact_address = str(conversation.contact_address or "").strip()
    contacts: list[LeadContactObservation] = []
    if contact_type is not None:
        provider = None
        provider_account_id = None
        external_subject_id = None
        if channel_type in {
            InboxChannelType.facebook_messenger.value,
            InboxChannelType.instagram_dm.value,
        }:
            provider = str(metadata.get("provider") or "team_inbox")
            provider_account_id = str(
                metadata.get("external_account_id")
                or metadata.get("page_id")
                or metadata.get("instagram_account_id")
                or channel_type
            )
            external_subject_id = contact_address
        contacts.append(
            LeadContactObservation(
                channel_type=contact_type,
                value=contact_address,
                display_value=contact_address,
                provider=provider,
                provider_account_id=provider_account_id,
                external_subject_id=external_subject_id,
                is_primary=True,
            )
        )

    clean_title = (
        str(title or "").strip()
        or conversation.subject
        or f"{channel_type.replace('_', ' ').title()} inbox enquiry"
    )
    payload = LeadCaptureRequest(
        party=LeadCapturePartyCreate(
            display_name=contact_address,
            contacts=contacts,
        ),
        title=clean_title[:200],
        lead_source=_lead_source_for_channel(channel_type),
        origin=LeadOriginCaptureCreate(
            capture_method="agent_declared",
            source_platform="agent",
            source_interaction_id=f"team-inbox:{conversation.id}",
            capture_source="admin_inbox",
            capture_reason="Operator created a lead from an unmatched inbox conversation",
        ),
        notes=str(note or "").strip() or None,
    )
    try:
        result = sales_capture.capture_lead(
            db,
            payload,
            actor_id=str(actor_person_id or "system:team-inbox-admin"),
            commit=False,
        )
    except sales_capture.LeadCaptureError as exc:
        raise InboxCommandRejected(str(exc), conversation_id=conversation.id) from exc
    metadata["lead_capture"] = {
        "lead_id": str(result.lead.id),
        "party_id": str(result.party_id),
        "origin_capture_id": str(result.origin.id),
        "source": "admin_inbox",
        "actor_person_id": str(actor_person_id or "") or None,
        "replayed": result.replayed,
    }
    conversation.metadata_ = metadata
    db.flush()
    return LeadCreationOutcome(
        conversation_id=str(conversation.id),
        lead_id=str(result.lead.id),
        party_id=str(result.party_id),
        replayed=result.replayed,
    )


def _organization_party_for_reseller(db: Session, reseller: Reseller) -> Party:
    if reseller.party_id is not None:
        party = db.get(Party, reseller.party_id)
        if party is None:
            raise InboxCommandRejected("Reseller Party binding is missing.")
        return party
    party = party_service.create_party(
        db,
        party_type=PartyType.organization,
        display_name=reseller.name,
        metadata={"created_by": "team_inbox_lead_merge"},
    )
    party_service.bind_reseller_profile(
        db,
        reseller_id=reseller.id,
        party_id=party.id,
        source="team_inbox_lead_merge",
        reason="Reviewed Inbox Lead association to reseller profile",
    )
    return party


def _organization_party_for_business(db: Session, organization: Organization) -> Party:
    if organization.party_id is not None:
        party = db.get(Party, organization.party_id)
        if party is None:
            raise InboxCommandRejected("Business Party binding is missing.")
        return party
    party = party_service.create_party(
        db,
        party_type=PartyType.organization,
        display_name=organization.name,
        metadata={"created_by": "team_inbox_lead_merge"},
    )
    party_service.bind_organization_profile(
        db,
        organization_id=organization.id,
        party_id=party.id,
        source="team_inbox_lead_merge",
        reason="Reviewed Inbox Lead association to business profile",
    )
    return party


def _relate_lead_party_to_target(
    db: Session,
    *,
    lead: Lead,
    target_party: Party,
) -> None:
    if lead.party_id == target_party.id:
        return
    if db.get(Party, lead.party_id) is None:
        raise InboxCommandRejected("Lead Party is missing.")
    duplicate = (
        db.query(PartyRelationship.id)
        .filter(
            PartyRelationship.subject_party_id == lead.party_id,
            PartyRelationship.object_party_id == target_party.id,
            PartyRelationship.relationship_type
            == PartyRelationshipType.contact_for.value,
        )
        .scalar()
    )
    if duplicate is None:
        party_service.relate_parties(
            db,
            subject_party_id=lead.party_id,
            object_party_id=target_party.id,
            relationship_type=PartyRelationshipType.contact_for,
            source="team_inbox_lead_merge",
            metadata={"lead_id": str(lead.id)},
        )


def _merge_conversation_lead_uncommitted(
    db: Session,
    *,
    conversation: InboxConversation,
    lead: Lead,
    target_type: str,
    actor_person_id: str | UUID | None = None,
    subscriber: Subscriber | None = None,
    reseller: Reseller | None = None,
    organization: Organization | None = None,
) -> LeadMergeOutcome:
    actor = str(actor_person_id or "system:team-inbox-admin")
    if target_type == "subscriber":
        if subscriber is None:
            raise InboxCommandRejected(
                "Subscriber target is required.",
                conversation_id=conversation.id,
            )
        try:
            account_conversion.convert_lead_account(
                db,
                lead_id=lead.id,
                party_id=lead.party_id,
                subscriber_id=subscriber.id,
                actor_id=actor,
                commit=False,
            )
        except account_conversion.LeadAccountConversionError as exc:
            raise InboxCommandRejected(
                str(exc),
                conversation_id=conversation.id,
            ) from exc
        if conversation.contact_address:
            team_inbox_contact_links.link_conversation_contact(
                db,
                conversation=conversation,
                subscriber_id=subscriber.id,
                linked_by_person_id=actor_person_id,
                note="Merged Inbox Lead into customer account",
            )
        target_payload = {
            "target_type": "subscriber",
            "target_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        }
        outcome = LeadMergeOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(lead.id),
            target_type="subscriber",
            target_id=str(subscriber.id),
            subscriber_id=str(subscriber.id),
        )
    elif target_type == "reseller":
        if reseller is None:
            raise InboxCommandRejected(
                "Reseller target is required.",
                conversation_id=conversation.id,
            )
        target_party = _organization_party_for_reseller(db, reseller)
        party_service.ensure_role(
            db,
            party_id=target_party.id,
            role_type=PartyRoleType.reseller,
            status=PartyRoleStatus.active,
            source="team_inbox_lead_merge",
        )
        _relate_lead_party_to_target(db, lead=lead, target_party=target_party)
        if conversation.contact_address:
            team_inbox_contact_links.link_conversation_contact(
                db,
                conversation=conversation,
                reseller_id=reseller.id,
                linked_by_person_id=actor_person_id,
                note="Merged Inbox Lead into reseller profile",
            )
        target_payload = {
            "target_type": "reseller",
            "target_id": str(reseller.id),
            "reseller_id": str(reseller.id),
            "target_party_id": str(target_party.id),
        }
        outcome = LeadMergeOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(lead.id),
            target_type="reseller",
            target_id=str(reseller.id),
            reseller_id=str(reseller.id),
        )
    elif target_type == "organization":
        if organization is None:
            raise InboxCommandRejected(
                "Business target is required.",
                conversation_id=conversation.id,
            )
        target_party = _organization_party_for_business(db, organization)
        party_service.ensure_role(
            db,
            party_id=target_party.id,
            role_type=PartyRoleType.customer,
            status=PartyRoleStatus.active,
            source="team_inbox_lead_merge",
        )
        _relate_lead_party_to_target(db, lead=lead, target_party=target_party)
        target_payload = {
            "target_type": "organization",
            "target_id": str(organization.id),
            "organization_id": str(organization.id),
            "target_party_id": str(target_party.id),
        }
        outcome = LeadMergeOutcome(
            conversation_id=str(conversation.id),
            lead_id=str(lead.id),
            target_type="organization",
            target_id=str(organization.id),
            organization_id=str(organization.id),
        )
    else:
        raise InboxCommandError(
            "Choose whether this lead belongs to a subscriber, reseller, or business."
        )

    metadata = dict(conversation.metadata_ or {})
    lead_capture = dict(metadata.get("lead_capture") or {})
    lead_metadata = dict(lead.metadata_ or {})
    lead_metadata["inbox_merge"] = {
        **target_payload,
        "conversation_id": str(conversation.id),
        "actor_person_id": str(actor_person_id or "") or None,
        "source": "admin_inbox",
    }
    lead.metadata_ = lead_metadata
    lead_capture["merge"] = lead_metadata["inbox_merge"]
    metadata["lead_capture"] = lead_capture
    conversation.metadata_ = metadata
    db.flush()
    return outcome


def merge_conversation_lead(
    db: Session,
    *,
    conversation_id: str | UUID,
    target_type: str,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    organization_id: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
) -> LeadMergeOutcome:
    clean_target = str(target_type or "").strip().lower()

    def action() -> LeadMergeOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        lead = _lead_from_conversation_metadata(db, conversation)
        metadata = dict(conversation.metadata_ or {})
        lead_capture = dict(metadata.get("lead_capture") or {})
        actor = str(actor_person_id or "system:team-inbox-admin")

        if clean_target == "subscriber":
            target_uuid = coerce_uuid(subscriber_id)
            subscriber = db.get(Subscriber, target_uuid) if target_uuid else None
            if subscriber is None:
                raise InboxCommandRejected(
                    "Subscriber target is required.",
                    conversation_id=conversation.id,
                )
            try:
                account_conversion.convert_lead_account(
                    db,
                    lead_id=lead.id,
                    party_id=lead.party_id,
                    subscriber_id=subscriber.id,
                    actor_id=actor,
                    commit=False,
                )
            except account_conversion.LeadAccountConversionError as exc:
                raise InboxCommandRejected(
                    str(exc),
                    conversation_id=conversation.id,
                ) from exc
            if conversation.contact_address:
                team_inbox_contact_links.link_conversation_contact(
                    db,
                    conversation=conversation,
                    subscriber_id=subscriber.id,
                    linked_by_person_id=actor_person_id,
                    note="Merged Inbox Lead into customer account",
                )
            target_payload = {
                "target_type": "subscriber",
                "target_id": str(subscriber.id),
                "subscriber_id": str(subscriber.id),
            }
            outcome = LeadMergeOutcome(
                conversation_id=str(conversation.id),
                lead_id=str(lead.id),
                target_type="subscriber",
                target_id=str(subscriber.id),
                subscriber_id=str(subscriber.id),
            )
        elif clean_target == "reseller":
            target_uuid = coerce_uuid(reseller_id)
            reseller = db.get(Reseller, target_uuid) if target_uuid else None
            if reseller is None:
                raise InboxCommandRejected(
                    "Reseller target is required.",
                    conversation_id=conversation.id,
                )
            target_party = _organization_party_for_reseller(db, reseller)
            party_service.ensure_role(
                db,
                party_id=target_party.id,
                role_type=PartyRoleType.reseller,
                status=PartyRoleStatus.active,
                source="team_inbox_lead_merge",
            )
            _relate_lead_party_to_target(db, lead=lead, target_party=target_party)
            if conversation.contact_address:
                team_inbox_contact_links.link_conversation_contact(
                    db,
                    conversation=conversation,
                    reseller_id=reseller.id,
                    linked_by_person_id=actor_person_id,
                    note="Merged Inbox Lead into reseller profile",
                )
            target_payload = {
                "target_type": "reseller",
                "target_id": str(reseller.id),
                "reseller_id": str(reseller.id),
                "target_party_id": str(target_party.id),
            }
            outcome = LeadMergeOutcome(
                conversation_id=str(conversation.id),
                lead_id=str(lead.id),
                target_type="reseller",
                target_id=str(reseller.id),
                reseller_id=str(reseller.id),
            )
        elif clean_target == "organization":
            target_uuid = coerce_uuid(organization_id)
            organization = db.get(Organization, target_uuid) if target_uuid else None
            if organization is None:
                raise InboxCommandRejected(
                    "Business target is required.",
                    conversation_id=conversation.id,
                )
            target_party = _organization_party_for_business(db, organization)
            party_service.ensure_role(
                db,
                party_id=target_party.id,
                role_type=PartyRoleType.customer,
                status=PartyRoleStatus.active,
                source="team_inbox_lead_merge",
            )
            _relate_lead_party_to_target(db, lead=lead, target_party=target_party)
            target_payload = {
                "target_type": "organization",
                "target_id": str(organization.id),
                "organization_id": str(organization.id),
                "target_party_id": str(target_party.id),
            }
            outcome = LeadMergeOutcome(
                conversation_id=str(conversation.id),
                lead_id=str(lead.id),
                target_type="organization",
                target_id=str(organization.id),
                organization_id=str(organization.id),
            )
        else:
            raise InboxCommandError(
                "Choose whether this lead belongs to a subscriber, reseller, or business."
            )

        lead_metadata = dict(lead.metadata_ or {})
        lead_metadata["inbox_merge"] = {
            **target_payload,
            "conversation_id": str(conversation.id),
            "actor_person_id": str(actor_person_id or "") or None,
            "source": "admin_inbox",
        }
        lead.metadata_ = lead_metadata
        lead_capture["merge"] = lead_metadata["inbox_merge"]
        metadata["lead_capture"] = lead_capture
        conversation.metadata_ = metadata
        db.flush()
        return outcome

    return _commit(db, action)


def merge_contact(
    db: Session,
    *,
    conversation_id: str | UUID,
    target_type: str,
    target_query: str | None = None,
    actor_person_id: str | UUID | None = None,
) -> ContactMergeOutcome:
    clean_target = str(target_type or "").strip().lower()
    clean_query = str(target_query or "").strip()

    def action() -> ContactMergeOutcome:
        conversation = _active_conversation(db, conversation_id, for_update=True)
        if clean_target == "lead":
            created = create_lead_from_conversation_uncommitted(
                db,
                conversation=conversation,
                actor_person_id=actor_person_id,
                title=None,
                note="Created from Team Inbox contact merge",
            )
            return ContactMergeOutcome(
                conversation_id=str(conversation.id),
                target_type="lead",
                target_id=created.lead_id,
                lead_id=created.lead_id,
            )
        if not clean_query:
            raise InboxCommandRejected(
                "Search for the customer, reseller, or business to merge into.",
                conversation_id=conversation.id,
            )
        lead = _lead_from_conversation_or_create(
            db,
            conversation=conversation,
            actor_person_id=actor_person_id,
        )
        if clean_target == "subscriber":
            subscriber = _search_subscriber(db, clean_query)
            outcome = _merge_conversation_lead_uncommitted(
                db,
                conversation=conversation,
                lead=lead,
                target_type="subscriber",
                subscriber=subscriber,
                actor_person_id=actor_person_id,
            )
        elif clean_target == "reseller":
            reseller = _search_reseller(db, clean_query)
            outcome = _merge_conversation_lead_uncommitted(
                db,
                conversation=conversation,
                lead=lead,
                target_type="reseller",
                reseller=reseller,
                actor_person_id=actor_person_id,
            )
        elif clean_target in {"organization", "business"}:
            organization = _search_organization(db, clean_query)
            outcome = _merge_conversation_lead_uncommitted(
                db,
                conversation=conversation,
                lead=lead,
                target_type="organization",
                organization=organization,
                actor_person_id=actor_person_id,
            )
        else:
            raise InboxCommandError(
                "Choose whether to create a lead or merge to a customer, reseller, or business."
            )
        return ContactMergeOutcome(
            conversation_id=outcome.conversation_id,
            target_type=outcome.target_type,
            target_id=outcome.target_id,
            lead_id=outcome.lead_id,
        )

    return _commit(db, action)


def create_internal_note(
    db: Session,
    *,
    conversation_id: str | UUID,
    body: str,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        team_inbox_operations.create_internal_note(
            db,
            conversation=_active_conversation(db, conversation_id),
            body=body,
            actor_person_id=actor_person_id,
        )

    _commit(db, action)


def create_comment(
    db: Session,
    *,
    conversation_id: str | UUID,
    body: str,
    message_id: str | UUID | None = None,
    actor_person_id: str | UUID | None = None,
) -> None:
    def action() -> None:
        team_inbox_operations.create_comment(
            db,
            conversation=_active_conversation(db, conversation_id),
            body=body,
            message_id=message_id,
            author_person_id=actor_person_id,
        )

    _commit(db, action)


def resolve_comment(
    db: Session,
    *,
    comment_id: str | UUID,
    actor_person_id: str | UUID | None = None,
) -> str:
    def action() -> str:
        comment = team_inbox_operations.resolve_comment(
            db,
            comment_id=comment_id,
            resolved_by_person_id=actor_person_id,
        )
        return str(comment.conversation_id)

    return _commit(db, action)


def update_status(
    db: Session,
    *,
    conversation_id: str | UUID,
    status_value: str,
    actor_person_id: str | UUID | None = None,
) -> StatusOutcome:
    clean_status = str(status_value or "").strip().lower()
    allowed_statuses = {item.value for item in InboxConversationStatus}
    if clean_status not in allowed_statuses:
        raise InboxCommandError("Unsupported conversation status.")

    def action() -> StatusOutcome:
        conversation = _active_conversation(db, conversation_id)
        previous_status = conversation.status
        if previous_status == clean_status:
            return StatusOutcome(
                conversation_id=str(conversation.id),
                status=clean_status,
                already_set=True,
            )
        metadata = dict(conversation.metadata_ or {})
        history = metadata.get("status_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "from": previous_status,
                "to": clean_status,
                "at": datetime.now(UTC).isoformat(),
                "actor_id": str(actor_person_id) if actor_person_id else None,
                "source": "admin_inbox_status_action",
            }
        )
        metadata["status_history"] = history[-50:]
        conversation.status = clean_status
        conversation.metadata_ = metadata
        return StatusOutcome(
            conversation_id=str(conversation.id),
            status=clean_status,
            already_set=False,
        )

    return _commit(db, action)
