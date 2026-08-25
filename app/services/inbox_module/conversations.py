"""Sub's only door to `dotmac_inbox`.

The module's service functions take a `tenant_id` and no scope object, own no
transaction, and derive threading identity themselves. This facade supplies the
operator tenant, builds the identity from Sub's vocabulary, and leaves the
transaction where it already is — Sub's session owner keeps commit, exactly as
the module's docstring asks ("callers own authorization and transactions").

Nothing here decides anything. If a rule appears in this file, it belongs either
in `dotmac_inbox` (if it is a conversation rule) or in the calling
`team_inbox_*` service (if it is a Sub rule). The facade's whole job is
translation, and keeping it that way is what makes "one owner per decision"
checkable.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from dotmac_inbox.lifecycle import Direction, Status
from dotmac_inbox.models import Conversation, ConversationReadState, Message
from dotmac_inbox.service import (
    ConversationConflict,
    ConversationNotFound,
    StaleConversationState,
)
from dotmac_inbox.service import (
    create_conversation as _create_conversation,
)
from dotmac_inbox.service import (
    mark_conversation_read as _mark_conversation_read,
)
from dotmac_inbox.service import (
    record_message as _record_message,
)
from dotmac_inbox.service import (
    transition_conversation_status as _transition_conversation_status,
)
from sqlalchemy.orm import Session

from app.services.inbox_module.references import inbound_identity
from app.services.operator_tenant import operator_tenant_id

__all__ = [
    "Conversation",
    "ConversationConflict",
    "ConversationNotFound",
    "ConversationReadState",
    "Direction",
    "Message",
    "StaleConversationState",
    "Status",
    "mark_read",
    "open_conversation",
    "record_message",
    "transition_status",
]


def open_conversation(
    db: Session,
    *,
    channel: str,
    account_scope: str,
    contact: str,
    external_thread_id: str | None = None,
    subject: str | None = None,
    tags: tuple[str, ...] = (),
    status: Status = Status.OPEN,
    reason: str | None = None,
) -> Conversation:
    """Open a thread, or return the durable winner for the same identity.

    Idempotent by thread key inside the module, including under a concurrent
    insert — which is why a Sub caller must not first check for an existing
    conversation and then create one. Call this and use what comes back.
    """
    return _create_conversation(
        db,
        tenant_id=operator_tenant_id(),
        identity=inbound_identity(
            channel=channel,
            account_scope=account_scope,
            contact=contact,
            external_thread_id=external_thread_id,
            subject=subject,
        ),
        status=status,
        reason=reason,
        subject=subject,
        tags=tags,
    )


def record_message(
    db: Session,
    *,
    conversation_id: uuid.UUID,
    channel: str,
    account_scope: str,
    contact: str,
    direction: Direction,
    occurred_at: datetime,
    external_thread_id: str | None = None,
    external_message_id: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    author_id: uuid.UUID | None = None,
    transport_observation_ref: str | None = None,
) -> Message:
    """Append one message and let the module update conversation activity.

    `subject` and `body` are passed even when the channel declares a provider
    message id, because a provider that omits the id on a particular message
    falls back to the content fingerprint — and a fingerprint over `None` would
    collapse every such message onto one key.
    """
    return _record_message(
        db,
        tenant_id=operator_tenant_id(),
        conversation_id=conversation_id,
        identity=inbound_identity(
            channel=channel,
            account_scope=account_scope,
            contact=contact,
            external_thread_id=external_thread_id,
            external_message_id=external_message_id,
            subject=subject,
            body=body,
        ),
        direction=direction,
        occurred_at=occurred_at,
        author_id=author_id,
        transport_observation_ref=transport_observation_ref,
    )


def transition_status(
    db: Session,
    *,
    conversation_id: uuid.UUID,
    expected: Status,
    requested: Status,
    reason: str | None,
    occurred_at: datetime,
    snoozed_until: datetime | None = None,
) -> Conversation:
    """Apply an expected-state transition, raising on a stale expectation.

    `expected` is not ceremony. Two operators resolving the same conversation
    from two screens is the ordinary case in a staffed inbox, and the module
    refuses the second with `StaleConversationState` rather than letting the
    later write win silently.
    """
    return _transition_conversation_status(
        db,
        tenant_id=operator_tenant_id(),
        conversation_id=conversation_id,
        expected=expected,
        requested=requested,
        reason=reason,
        occurred_at=occurred_at,
        snoozed_until=snoozed_until,
    )


def mark_read(
    db: Session,
    *,
    conversation_id: uuid.UUID,
    actor_id: uuid.UUID,
    through_message_id: uuid.UUID | None,
    read_at: datetime,
) -> ConversationReadState:
    """Advance one operator's cursor. Monotonic inside the module."""
    return _mark_conversation_read(
        db,
        tenant_id=operator_tenant_id(),
        conversation_id=conversation_id,
        actor_id=actor_id,
        through_message_id=through_message_id,
        read_at=read_at,
    )
