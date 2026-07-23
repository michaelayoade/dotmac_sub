"""Canonical operator read cursors and unread projection for Team Inbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationReadState,
    InboxMessage,
    InboxMessageDirection,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_operator_state"
_MARK_READ = OwnerCommandDefinition(
    owner=OWNER,
    concern="operator read cursor",
    name="mark_team_inbox_conversation_read",
)
_REBUILD = OwnerCommandDefinition(
    owner=OWNER,
    concern="operator unread projection repair",
    name="rebuild_team_inbox_operator_read_state",
)


class TeamInboxReadStateError(DomainError):
    """Stable operator-state error mapped only by adapters."""


@dataclass(frozen=True, slots=True)
class MarkConversationReadCommand:
    context: CommandContext
    conversation_id: UUID
    person_id: UUID
    through_message_id: UUID | None = None
    read_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RebuildOperatorReadStateCommand:
    context: CommandContext
    person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConversationReadOutcome:
    conversation_id: UUID
    person_id: UUID
    through_message_id: UUID | None
    last_read_at: datetime
    changed: bool
    command_id: UUID


@dataclass(frozen=True, slots=True)
class ReadStateRepairOutcome:
    inspected: int
    repaired: int


def _error(suffix: str, message: str, **details: object) -> TeamInboxReadStateError:
    return TeamInboxReadStateError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _latest_message(db: Session, conversation_id: UUID) -> InboxMessage | None:
    return db.execute(
        select(InboxMessage)
        .where(InboxMessage.conversation_id == conversation_id)
        .order_by(
            InboxMessage.received_at.desc().nullslast(),
            InboxMessage.sent_at.desc().nullslast(),
            InboxMessage.created_at.desc(),
            InboxMessage.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def mark_conversation_read(
    db: Session,
    command: MarkConversationReadCommand,
) -> ConversationReadOutcome:
    """Advance one operator cursor; retries never move the cursor backwards."""

    def operation() -> ConversationReadOutcome:
        conversation = db.execute(
            select(InboxConversation)
            .where(InboxConversation.id == command.conversation_id)
            .with_for_update()
        ).scalar_one_or_none()
        if conversation is None or not conversation.is_active:
            raise _error(
                "conversation_not_found",
                "Inbox conversation was not found.",
                conversation_id=str(command.conversation_id),
            )
        message = (
            db.get(InboxMessage, command.through_message_id)
            if command.through_message_id is not None
            else _latest_message(db, conversation.id)
        )
        if message is not None and message.conversation_id != conversation.id:
            raise _error(
                "message_scope_mismatch",
                "Read cursor message does not belong to the conversation.",
            )
        read_at = command.read_at or datetime.now(UTC)
        if read_at.tzinfo is None:
            raise _error(
                "invalid_read_time", "Read cursor time must be timezone-aware."
            )
        state = db.execute(
            select(InboxConversationReadState)
            .where(
                InboxConversationReadState.conversation_id == conversation.id,
                InboxConversationReadState.person_id == command.person_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            state = InboxConversationReadState(
                conversation_id=conversation.id,
                person_id=command.person_id,
                last_read_message_id=message.id if message is not None else None,
                last_read_at=read_at.astimezone(UTC),
            )
            db.add(state)
            changed = True
        elif read_at.astimezone(UTC) <= _utc(state.last_read_at):
            changed = False
        else:
            state.last_read_at = read_at.astimezone(UTC)
            state.last_read_message_id = message.id if message is not None else None
            changed = True
        db.flush()
        return ConversationReadOutcome(
            conversation_id=conversation.id,
            person_id=command.person_id,
            through_message_id=state.last_read_message_id,
            last_read_at=state.last_read_at,
            changed=changed,
            command_id=command.context.command_id,
        )

    return execute_owner_command(
        db,
        definition=_MARK_READ,
        context=command.context,
        operation=operation,
    )


def conversation_is_unread(
    db: Session,
    *,
    conversation_id: UUID,
    person_id: UUID,
) -> bool:
    last_inbound_at = db.execute(
        select(InboxMessage.received_at)
        .where(
            InboxMessage.conversation_id == conversation_id,
            InboxMessage.direction == InboxMessageDirection.inbound.value,
        )
        .order_by(
            InboxMessage.received_at.desc().nullslast(),
            InboxMessage.created_at.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()
    if last_inbound_at is None:
        return False
    state = db.execute(
        select(InboxConversationReadState).where(
            InboxConversationReadState.conversation_id == conversation_id,
            InboxConversationReadState.person_id == person_id,
        )
    ).scalar_one_or_none()
    return state is None or _utc(state.last_read_at) < _utc(last_inbound_at)


def unread_conversation_count(db: Session, *, person_id: UUID) -> int:
    conversation_ids = db.scalars(
        select(InboxConversation.id).where(InboxConversation.is_active.is_(True))
    ).all()
    return sum(
        conversation_is_unread(db, conversation_id=conversation_id, person_id=person_id)
        for conversation_id in conversation_ids
    )


def rebuild_operator_read_state(
    db: Session,
    command: RebuildOperatorReadStateCommand,
) -> ReadStateRepairOutcome:
    """Idempotently clear impossible cross-conversation message cursors."""

    def operation() -> ReadStateRepairOutcome:
        query = select(InboxConversationReadState).with_for_update()
        if command.person_id is not None:
            query = query.where(
                InboxConversationReadState.person_id == command.person_id
            )
        states = list(db.scalars(query).all())
        repaired = 0
        for state in states:
            if state.last_read_message_id is None:
                continue
            message = db.get(InboxMessage, state.last_read_message_id)
            if message is not None and message.conversation_id == state.conversation_id:
                continue
            state.last_read_message_id = None
            repaired += 1
        db.flush()
        return ReadStateRepairOutcome(inspected=len(states), repaired=repaired)

    return execute_owner_command(
        db,
        definition=_REBUILD,
        context=command.context,
        operation=operation,
    )
