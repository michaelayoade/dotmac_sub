"""Transactional projection of the latest qualifying Meta customer inbound."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.models.team_inbox import InboxConversation

OWNER = "communications.team_inbox_threads"

META_FREE_FORM_CHANNELS = frozenset(
    {
        "whatsapp",
        "facebook_messenger",
        "instagram_dm",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectQualifyingInboundCommand:
    conversation_id: UUID
    occurred_at: datetime
    message_metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProjectQualifyingInboundOutcome:
    changed: bool
    projected_at: datetime | None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def projects_reply_window(
    *, channel_type: str, message_metadata: Mapping[str, object]
) -> bool:
    """Return whether one inbound message advances the Meta reply window."""

    return (
        channel_type in META_FREE_FORM_CHANNELS
        and message_metadata.get("reply_window_qualifying") is not False
    )


def project_qualifying_inbound(
    conversation: InboxConversation,
    command: ProjectQualifyingInboundCommand,
) -> ProjectQualifyingInboundOutcome:
    """Advance the projection monotonically inside the thread owner's transaction."""

    if conversation.id != command.conversation_id:
        raise ValueError("Conversation identity does not match projection command")
    if not projects_reply_window(
        channel_type=str(conversation.channel_type or ""),
        message_metadata=command.message_metadata,
    ):
        return ProjectQualifyingInboundOutcome(
            changed=False,
            projected_at=conversation.last_qualifying_inbound_at,
        )

    occurred_at = _aware(command.occurred_at)
    current = conversation.last_qualifying_inbound_at
    if current is not None and _aware(current) >= occurred_at:
        return ProjectQualifyingInboundOutcome(changed=False, projected_at=current)

    conversation.last_qualifying_inbound_at = occurred_at
    return ProjectQualifyingInboundOutcome(changed=True, projected_at=occurred_at)
