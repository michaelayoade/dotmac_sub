"""Every inbox write, and the ONE place that knows which system owns it.

ADR-0013 P4/P5. The nine `team_inbox_*` modules that used to assign
module-owned columns call these functions instead. The stage branch
(`app.services.inbox_authority`) appears here and nowhere else — a branch
repeated at twenty-three call sites is twenty-three chances to get the
LOCAL/SHADOW/MODULE distinction subtly wrong, and the ones that are wrong will
be the rarely-exercised paths nobody notices until cutover day.

## The identity ordering, which is the load-bearing part

At SHADOW and MODULE the MODULE IS CALLED FIRST and Sub's row adopts the id it
minted. That is what keeps `mod_inbox.conversations.id` equal to
`public.inbox_conversations.id` for rows created after the switch, exactly as
the backfill keeps it for rows created before — so the drift comparator can
keep joining by primary key and never needs a mapping table.

The alternative — Sub mints, module mints its own — was rejected: it produces
two ids for one conversation from the first shadow write onward, and every
subsequent comparison reports the whole inbox as simultaneously missing and
orphaned.

## Failure policy, which differs by stage on purpose

- **SHADOW** — a module write that raises is logged and swallowed. A contact
  centre losing an inbound WhatsApp message to a shadow-path bug is a far worse
  outcome than the shadow window running another week, and the drift comparator
  will show the gap anyway.
- **MODULE** — the module is the authority, so its refusal IS the answer and
  propagates. Swallowing here would accept a message into Sub that the owner
  rejected, which is the fork the whole cutover exists to prevent.

That asymmetry is the reason the stage is passed around as an object with named
properties rather than a boolean.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
)
from app.services.inbox_account_scope import (
    AccountScopeUnresolved,
    resolve_account_scope,
)
from app.services.inbox_authority import InboxAuthorityStage, resolve_stage
from app.services.inbox_module import conversations as module_conversations
from app.services.inbox_module.conversations import Direction, Status

logger = logging.getLogger(__name__)


class MissingConversationContact(ValueError):
    """A conversation or message reached MODULE stage with no external party.

    The module threads on `(channel, account_scope, contact)`, so a blank
    contact has no thread key and cannot be admitted. Sub allows it — a widget
    session exists before the visitor identifies themselves — which is a real
    gap between the two models, recorded as the third item in ADR-0013 § 6a.

    Refusing is deliberate. The alternatives were both worse: an empty-string
    contact makes every anonymous visitor one shared thread, and a synthesised
    sentinel makes each one a thread that no later identification can merge.
    The module needs a typed internal-principal identity before these flows can
    cut over.
    """

    def __init__(self, *, channel: str, entity: str) -> None:
        super().__init__(
            f"{entity} on channel {channel!r} has no contact, so the composed "
            "inbox cannot thread it. See ADR-0013 § 6a — this is refused rather "
            "than admitted under an empty or synthesised identity."
        )
        self.channel = channel
        self.entity = entity


__all__ = [
    "MissingConversationContact",
    "clear_snooze",
    "open_conversation",
    "record_message",
    "set_contact",
    "set_status",
    "touch_activity",
]

#: Sub statuses and module statuses are the same four strings, verified in
#: ADR-0013's context section. The mapping is still explicit rather than a cast,
#: so a divergence upstream is a KeyError here and not a silently accepted
#: unknown status.
_STATUS = {
    InboxConversationStatus.open.value: Status.OPEN,
    InboxConversationStatus.pending.value: Status.PENDING,
    InboxConversationStatus.snoozed.value: Status.SNOOZED,
    InboxConversationStatus.resolved.value: Status.RESOLVED,
}


def _module_status(value: str) -> Status:
    try:
        return _STATUS[value]
    except KeyError:
        raise ValueError(
            f"conversation status {value!r} has no module counterpart; the two "
            "vocabularies are supposed to be identical (ADR-0013 context)"
        ) from None


def _scope_for(
    db,
    *,
    channel: str,
    service_team_id: uuid.UUID | None,
    provider_account_scope: str | None,
    stage: InboxAuthorityStage,
) -> str | None:
    """The connected account, or `None` when the module write must be skipped.

    At MODULE an unresolved scope RAISES: the module cannot thread the message
    without it, and threading it wrongly is worse than refusing. Before MODULE
    it is a skip, because Sub still owns the write and the backfill census is
    the place where unresolved scopes are meant to be counted and fixed.
    """
    scope = resolve_account_scope(
        channel=channel,
        service_team_id=service_team_id,
        provider_account_scope=provider_account_scope,
        db=db,
    )
    if scope is None:
        if stage.modules_are_authoritative:
            raise AccountScopeUnresolved(
                channel=channel, service_team_id=service_team_id
            )
        logger.warning(
            "inbox shadow write skipped: no account scope for channel %s team %s",
            channel,
            service_team_id,
        )
    return scope


def open_conversation(
    db,
    *,
    channel: str,
    contact: str | None,
    external_thread_id: str | None,
    subject: str | None,
    occurred_at: datetime,
    service_team_id: uuid.UUID | None = None,
    provider_account_scope: str | None = None,
    status: str = InboxConversationStatus.open.value,
    **sub_columns,
) -> InboxConversation:
    """Open a conversation, letting the owner of the moment mint its identity.

    `sub_columns` are Sub's own — `subscriber_id`, `metadata_`,
    `continued_from_conversation_id`, `priority`, and friends. The module has no
    opinion about them and never will, so they are passed straight through to
    the Sub row at every stage.
    """
    stage = resolve_stage(db)
    conversation_id: uuid.UUID | None = None

    if stage.modules_are_authoritative and not contact:
        raise MissingConversationContact(channel=channel, entity="conversation")

    if stage.writes_modules and contact:
        scope = _scope_for(
            db,
            channel=channel,
            service_team_id=service_team_id,
            provider_account_scope=provider_account_scope,
            stage=stage,
        )
        if scope is not None:
            try:
                module_row = module_conversations.open_conversation(
                    db,
                    channel=channel,
                    account_scope=scope,
                    contact=contact,
                    external_thread_id=external_thread_id,
                    subject=subject,
                    status=_module_status(status),
                )
                conversation_id = module_row.id
            except Exception:
                if stage.modules_are_authoritative:
                    raise
                logger.exception("inbox shadow conversation write failed")

    conversation = InboxConversation(
        channel_type=channel,
        status=status,
        subject=subject,
        contact_address=contact,
        external_thread_id=external_thread_id,
        first_message_at=occurred_at,
        last_message_at=occurred_at,
        **sub_columns,
    )
    if conversation_id is not None:
        conversation.id = conversation_id
    db.add(conversation)
    db.flush()
    return conversation


def touch_activity(
    db,
    *,
    conversation: InboxConversation,
    occurred_at: datetime,
    contact: str | None = None,
) -> None:
    """Record that a conversation saw traffic.

    At MODULE this is a NO-OP on the projected columns: `record_message` in the
    module already advances `first_message_at`/`last_message_at`, and the
    reconciler brings the answer back. Having Sub also write them would make two
    writers for one fact, which is the thing being retired.

    **One deliberate behaviour change.** Several callers previously advanced
    `last_message_at` alone and left `first_message_at` null forever if it had
    somehow never been set. This repairs it when it is null, which is what the
    module does unconditionally — so adopting the module's behaviour early keeps
    the shadow comparator from reporting a difference that is really Sub's old
    bug. It can only ever fill a hole; an existing value is never overwritten.
    """
    stage = resolve_stage(db)
    if stage.writes_sub_tables:
        conversation.last_message_at = occurred_at
        if conversation.first_message_at is None:
            conversation.first_message_at = occurred_at
        if contact and not conversation.contact_address:
            conversation.contact_address = contact


def record_message(
    db,
    *,
    conversation: InboxConversation,
    channel: str,
    direction: str,
    occurred_at: datetime,
    external_message_id: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    author_id: uuid.UUID | None = None,
    provider_account_scope: str | None = None,
    **sub_columns,
) -> InboxMessage:
    """Append a message, letting the owner of the moment mint its identity.

    The conversation's own `contact_address` is the external party for
    threading. Passing the message's `from_address` instead would be wrong for
    outbound messages, where the from-address is ours — and the module would
    then thread our own reply into a different conversation.
    """
    stage = resolve_stage(db)
    message_id: uuid.UUID | None = None
    contact = (conversation.contact_address or "").strip()

    if stage.modules_are_authoritative and not contact:
        raise MissingConversationContact(channel=channel, entity="message")

    if stage.writes_modules and contact:
        scope = _scope_for(
            db,
            channel=channel,
            service_team_id=conversation.primary_service_team_id,
            provider_account_scope=provider_account_scope,
            stage=stage,
        )
        if scope is not None:
            try:
                module_row = module_conversations.record_message(
                    db,
                    conversation_id=conversation.id,
                    channel=channel,
                    account_scope=scope,
                    contact=contact,
                    direction=Direction(direction),
                    occurred_at=occurred_at,
                    external_thread_id=conversation.external_thread_id,
                    external_message_id=external_message_id,
                    subject=subject,
                    body=body,
                    author_id=author_id,
                )
                message_id = module_row.id
            except Exception:
                if stage.modules_are_authoritative:
                    raise
                logger.exception("inbox shadow message write failed")

    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=channel,
        direction=direction,
        subject=subject,
        body=body,
        external_message_id=external_message_id,
        **sub_columns,
    )
    if message_id is not None:
        message.id = message_id
    db.add(message)
    db.flush()
    return message


def set_contact(db, *, conversation: InboxConversation, contact: str | None) -> None:
    """Fill in a contact address learned after the conversation was opened.

    Distinct from `touch_activity` on purpose: learning who we are talking to is
    not traffic, and folding it into the activity call would advance
    `last_message_at` for a conversation that saw no message.

    At MODULE it is a NO-OP. `contact` is part of the module's thread key, so
    changing it would re-thread the conversation; the module fixes it at open
    time and the reconciler projects that answer back.
    """
    if not contact:
        return
    if resolve_stage(db).writes_sub_tables and not conversation.contact_address:
        conversation.contact_address = contact


def clear_snooze(db, *, conversation: InboxConversation) -> None:
    """Drop a stale wake time without changing the lifecycle state.

    Sub clears `snoozed_until` on every wake path, including the one where the
    conversation was RESOLVED while asleep and therefore gets no status
    transition. That case is the reason this is its own operation rather than a
    parameter on `set_status`.

    At MODULE it is a NO-OP: the module nulls `snoozed_until` on every
    transition away from SNOOZED, so a non-snoozed module row already has none,
    and the reconciler carries that across. Writing Sub's column here would be a
    second writer for a fact the module already settled.
    """
    if resolve_stage(db).writes_sub_tables:
        conversation.snoozed_until = None


def set_status(
    db,
    *,
    conversation: InboxConversation,
    status: str,
    occurred_at: datetime,
    reason: str | None = None,
    snoozed_until: datetime | None = None,
) -> None:
    """Move a conversation's lifecycle state.

    At MODULE the module validates the transition against its table and refuses
    a stale expectation — two operators resolving the same conversation from two
    screens is ordinary in a staffed inbox, and the later write silently winning
    is the bug that behaviour prevents. Sub's own row is left to the reconciler.
    """
    stage = resolve_stage(db)

    if stage.writes_modules:
        try:
            module_conversations.transition_status(
                db,
                conversation_id=conversation.id,
                expected=_module_status(conversation.status),
                requested=_module_status(status),
                reason=reason,
                occurred_at=occurred_at,
                snoozed_until=snoozed_until,
            )
        except Exception:
            if stage.modules_are_authoritative:
                raise
            logger.exception("inbox shadow status write failed")

    if stage.writes_sub_tables:
        conversation.status = status
        conversation.snoozed_until = snoozed_until
