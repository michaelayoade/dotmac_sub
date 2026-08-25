"""Establish the composed inbox modules' history from Sub's own tables.

This is phase P3 of `docs/adr/0013-inbox-authority-cutover.md`. It reads
`public.inbox_*` and writes `mod_inbox.*` / `mod_inbox_ops.*`, keeping each
row's uuid (ADR-0013 § 2) so every existing foreign key, URL and saved filter
stays valid.

## Why this file writes module tables directly

It is the ONE declared exception to "Sub never writes a `mod_inbox*` table"
(ADR-0013, invariants). `dotmac_inbox.service.create_conversation` mints its own
uuid and takes no id argument, because minting identity is what a runtime entry
point is for — so importing history under the identity rule is not something the
module's API can express. The compromise is bounded: writes go through the
module's own MAPPED CLASSES, never raw SQL, so a column renamed upstream is an
ImportError or an AttributeError here rather than a silently skipped column.

## Census before apply

`census()` derives everything and writes nothing. `apply()` runs the census
first and REFUSES THE WHOLE RUN if it produced a single refusal. That ordering
is the point: a partial backfill of a live contact centre is worse than no
backfill, because the half that landed looks authoritative.

Both are restartable. A row already present on the module side is accepted only
when every imported fact agrees; same-id/different-fact is drift and refuses
the transaction rather than turning a partial or corrupt attempt into a replay.

The direct-model exception is temporary. Starter versions `dotmac-inbox`
0.1.0a2 and `dotmac-inbox-operations` 0.1.0a4 add the typed owner seams that
replace it, but source versions are not installable evidence. This bridge
retires when those exact releases are registry-verified and pinned here.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from dotmac_inbox.channels import channel_spec
from dotmac_inbox.models import Conversation, ConversationReadState, Message
from dotmac_inbox.threading import dedup_key, thread_key
from dotmac_inbox_operations.contracts import (
    AssignmentStatus,
    QueueEntryStatus,
)
from dotmac_inbox_operations.models import (
    ConversationAssignment,
    InboxQueue,
    InboxQueueEntry,
    InboxRoundRobinCursor,
)
from dotmac_inbox_operations.models import (
    InboxAgentPresence as ModuleAgentPresence,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxConversationReadState,
    InboxMessage,
    InboxProviderObservation,
    InboxQueueBinding,
    InboxTeamRoundRobinCursor,
)
from app.services.inbox_account_scope import (
    active_route_scopes,
    resolve_account_scope,
)
from app.services.inbox_module.references import (
    agent_reference,
    conversation_reference,
    presence_state,
)
from app.services.operator_tenant import operator_tenant_id

__all__ = [
    "BackfillDrift",
    "BackfillReport",
    "Refusal",
    "apply",
    "census",
    "derive_account_scope",
]

#: How many rows are loaded and flushed at a time. Large enough that 24k
#: conversations is a handful of round trips, small enough that the session's
#: identity map does not hold the whole inbox.
_BATCH: Final[int] = 500

#: Queue codes are `team-<uuid>`. A slug of the team name would read better and
#: would collide the first time two regions both have a "Support" team, and the
#: module's `code` is unique per tenant — so identity wins over prettiness. The
#: readable name is carried in the queue's `name`.
_QUEUE_CODE_PREFIX: Final = "team-"


@dataclass(frozen=True, slots=True)
class Refusal:
    """One row the backfill will not derive, named precisely enough to fix."""

    #: `account_scope`, `thread_key_collision`, `channel`, or `queue_binding`.
    kind: str
    entity: str
    entity_id: uuid.UUID | None
    detail: str


@dataclass
class BackfillReport:
    """What a run derived, what it wrote, and what it refused."""

    conversations: int = 0
    messages: int = 0
    read_states: int = 0
    queues: int = 0
    presence: int = 0
    assignments: int = 0
    queue_entries: int = 0
    cursors: int = 0
    written: int = 0
    refusals: list[Refusal] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return bool(self.refusals)

    def summary(self) -> str:
        counts = (
            f"conversations={self.conversations} messages={self.messages} "
            f"read_states={self.read_states} queues={self.queues} "
            f"presence={self.presence} assignments={self.assignments} "
            f"queue_entries={self.queue_entries} cursors={self.cursors} "
            f"written={self.written}"
        )
        if not self.refusals:
            return f"{counts} refusals=0"
        by_kind: dict[str, int] = defaultdict(int)
        for refusal in self.refusals:
            by_kind[refusal.kind] += 1
        breakdown = " ".join(
            f"{kind}={count}" for kind, count in sorted(by_kind.items())
        )
        return f"{counts} refusals={len(self.refusals)} ({breakdown})"


class BackfillRefused(RuntimeError):
    """`apply()` found refusals in its census and wrote nothing."""

    def __init__(self, report: BackfillReport) -> None:
        super().__init__(
            f"inbox backfill refused: {report.summary()}. No row was written. "
            "Resolve every refusal — see ADR-0013 § 5 — and run the census again."
        )
        self.report = report


class BackfillDrift(RuntimeError):
    """An existing target identity carries facts unlike the source row."""


def _instant(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _equal(current: Any, expected: Any) -> bool:
    if isinstance(current, datetime) and isinstance(expected, datetime):
        return _instant(current) == _instant(expected)
    return bool(current == expected)


def _require_exact(
    row: Any, *, entity: str, entity_id: object, expected: dict[str, Any]
) -> None:
    changed = sorted(
        name
        for name, value in expected.items()
        if not _equal(getattr(row, name), value)
    )
    if changed:
        raise BackfillDrift(
            f"existing {entity} {entity_id} differs from the derived source in "
            f"{', '.join(changed)}; the whole backfill must roll back"
        )


def _batched(rows: Iterable, size: int = _BATCH) -> Iterator[list]:
    batch: list = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# --------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------


def derive_account_scope(
    conversation: InboxConversation,
    *,
    observation_scope: str | None,
    route_scopes: dict[tuple[uuid.UUID, str], list[str]],
) -> str | None:
    """The ADR-0013 § 5 ladder, applied to one historical conversation.

    The ladder itself lives in `app.services.inbox_account_scope` because the
    RUNTIME write seam applies the same rule to live inbound messages. Two
    implementations would thread history and new traffic differently, which is
    the one drift a comparator joining by primary key cannot see.
    """
    return resolve_account_scope(
        channel=conversation.channel_type,
        service_team_id=conversation.primary_service_team_id,
        provider_account_scope=observation_scope,
        route_scopes=route_scopes,
    )


def _observation_scopes(db: Session) -> dict[uuid.UUID, str]:
    """The strongest `account_scope` evidence, one per conversation.

    Ordered by `observed_at` so the FIRST observation wins. The account a thread
    started at is the account it belongs to; a later observation on a migrated
    or re-connected account must not silently re-scope history.
    """
    rows = db.execute(
        select(
            InboxProviderObservation.conversation_id,
            InboxProviderObservation.provider_account_scope,
        )
        .where(InboxProviderObservation.conversation_id.is_not(None))
        .order_by(
            InboxProviderObservation.conversation_id,
            InboxProviderObservation.observed_at.asc(),
        )
    ).all()
    scopes: dict[uuid.UUID, str] = {}
    for conversation_id, scope in rows:
        if conversation_id is not None and conversation_id not in scopes and scope:
            scopes[conversation_id] = scope
    return scopes


def _conversation_contact(conversation: InboxConversation) -> str | None:
    """The external party. Empty is not a contact, and neither is whitespace."""
    contact = (conversation.contact_address or "").strip()
    return contact or None


def _occurred_at(message: InboxMessage) -> datetime:
    """`received_at` then `sent_at` then `created_at`.

    Total by construction: `created_at` is NOT NULL with a default, so no
    message is ever refused for want of a timestamp.
    """
    return message.received_at or message.sent_at or message.created_at


# --------------------------------------------------------------------------
# census
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _DerivedConversation:
    row: InboxConversation
    account_scope: str
    thread_key: str


def _derive_conversations(
    db: Session, report: BackfillReport
) -> dict[uuid.UUID, _DerivedConversation]:
    observation_scopes = _observation_scopes(db)
    route_scopes = active_route_scopes(db)
    derived: dict[uuid.UUID, _DerivedConversation] = {}
    seen_thread_keys: dict[str, uuid.UUID] = {}

    for conversation in db.scalars(select(InboxConversation)):
        report.conversations += 1

        try:
            channel_spec(conversation.channel_type)
        except Exception as exc:  # UnknownChannelError, deliberately broad
            report.refusals.append(
                Refusal(
                    kind="channel",
                    entity="inbox_conversations",
                    entity_id=conversation.id,
                    detail=str(exc),
                )
            )
            continue

        scope = derive_account_scope(
            conversation,
            observation_scope=observation_scopes.get(conversation.id),
            route_scopes=route_scopes,
        )
        if scope is None:
            report.refusals.append(
                Refusal(
                    kind="account_scope",
                    entity="inbox_conversations",
                    entity_id=conversation.id,
                    detail=(
                        f"channel={conversation.channel_type!r} "
                        f"team={conversation.primary_service_team_id} — no "
                        "observation, no single active route, and the channel "
                        "has an external transport"
                    ),
                )
            )
            continue

        contact = _conversation_contact(conversation)
        if contact is None and not conversation.external_thread_id:
            report.refusals.append(
                Refusal(
                    kind="account_scope",
                    entity="inbox_conversations",
                    entity_id=conversation.id,
                    detail="no contact_address and no external_thread_id, so the "
                    "conversation cannot be threaded at all",
                )
            )
            continue

        from app.services.inbox_module.references import inbound_identity

        try:
            key = thread_key(
                inbound_identity(
                    channel=conversation.channel_type,
                    account_scope=scope,
                    contact=contact or "",
                    external_thread_id=conversation.external_thread_id,
                )
            )
        except ValueError as exc:
            report.refusals.append(
                Refusal(
                    kind="thread_key",
                    entity="inbox_conversations",
                    entity_id=conversation.id,
                    detail=str(exc),
                )
            )
            continue

        collided_with = seen_thread_keys.get(key)
        if collided_with is not None:
            report.refusals.append(
                Refusal(
                    kind="thread_key_collision",
                    entity="inbox_conversations",
                    entity_id=conversation.id,
                    detail=(
                        f"derived thread_key {key!r} already claimed by "
                        f"{collided_with}. Merging two conversations is a data "
                        "decision, not a migration one — ADR-0013 § 5."
                    ),
                )
            )
            continue

        seen_thread_keys[key] = conversation.id
        derived[conversation.id] = _DerivedConversation(
            row=conversation, account_scope=scope, thread_key=key
        )

    return derived


def _derive_messages(
    db: Session,
    report: BackfillReport,
    conversations: dict[uuid.UUID, _DerivedConversation],
) -> list[tuple[InboxMessage, _DerivedConversation, str]]:
    from app.services.inbox_module.references import inbound_identity

    derived: list[tuple[InboxMessage, _DerivedConversation, str]] = []
    seen_keys: dict[str, uuid.UUID] = {}

    for message in db.scalars(select(InboxMessage).order_by(InboxMessage.created_at)):
        report.messages += 1
        parent = conversations.get(message.conversation_id)
        if parent is None:
            # Its conversation was refused above. Reporting the message too
            # would bury the one refusal that matters under its whole thread.
            continue

        key = dedup_key(
            inbound_identity(
                channel=message.channel_type,
                account_scope=parent.account_scope,
                contact=_conversation_contact(parent.row) or "",
                external_thread_id=message.external_thread_id,
                external_message_id=message.external_message_id,
                subject=message.subject,
                body=message.body,
            )
        ).value

        claimed = seen_keys.get(key)
        if claimed is not None:
            report.refusals.append(
                Refusal(
                    kind="message_key_collision",
                    entity="inbox_messages",
                    entity_id=message.id,
                    detail=(
                        f"derived message_key {key!r} already claimed by {claimed}. "
                        "Two messages deduplicating onto one key means the "
                        "channel's declared MessageIdScope disagrees with the "
                        "data — fix the declaration, not the row."
                    ),
                )
            )
            continue

        seen_keys[key] = message.id
        derived.append((message, parent, key))

    return derived


@dataclass(slots=True)
class _Derivation:
    """One pass over the source tables, shared by `census` and `apply`.

    Both need exactly the same derivation, and deriving twice over 24k
    conversations is not just slow — two passes could disagree if a row changed
    between them, and `apply` would then write something the census never
    approved.
    """

    report: BackfillReport
    conversations: dict[uuid.UUID, _DerivedConversation]
    messages: list[tuple[InboxMessage, _DerivedConversation, str]]


def _derive(db: Session) -> _Derivation:
    report = BackfillReport()
    conversations = _derive_conversations(db, report)
    messages = _derive_messages(db, report, conversations)

    # The operational tables are a plain count: none of them needs a derivation,
    # so none of them can be refused. Only conversations and messages can.
    report.read_states = _count(db, InboxConversationReadState)
    report.presence = _count(db, InboxAgentPresence)
    report.assignments = _count(db, InboxConversationAssignment)
    report.queue_entries = _count(db, InboxConversationQueueEntry)
    report.cursors = _count(db, InboxTeamRoundRobinCursor)
    report.queues = _count(db, ServiceTeam)
    return _Derivation(report=report, conversations=conversations, messages=messages)


def census(db: Session) -> BackfillReport:
    """Derive everything, write nothing, and report every refusal by id.

    This is the gate on P3. It is safe to run against production repeatedly and
    it holds no locks beyond its reads.
    """
    return _derive(db).report


def _count(db: Session, model) -> int:
    from sqlalchemy import func

    return int(db.scalar(select(func.count()).select_from(model)) or 0)


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def apply(db: Session) -> BackfillReport:
    """Write the derived history, or write nothing at all.

    Idempotent: a row already present on the module side is accepted only when
    every imported fact is equal. The caller owns the transaction — this
    function never commits, so a failure anywhere leaves the database exactly
    as it was found.
    """
    derivation = _derive(db)
    report = derivation.report
    if report.refused:
        raise BackfillRefused(report)

    tenant_id = operator_tenant_id()
    conversations = derivation.conversations

    written = 0
    written += _write_conversations(db, tenant_id, conversations)
    written += _write_messages(db, tenant_id, derivation.messages)
    written += _write_read_states(db, tenant_id, conversations)
    queue_by_team, queue_writes = _write_queues(db, tenant_id)
    written += queue_writes
    written += _write_presence(db, tenant_id)
    written += _write_assignments(db, tenant_id, queue_by_team)
    written += _write_queue_entries(db, tenant_id, queue_by_team)
    written += _write_cursors(db, tenant_id, queue_by_team)

    report.written = written
    return report


def _write_conversations(
    db: Session,
    tenant_id: uuid.UUID,
    conversations: dict[uuid.UUID, _DerivedConversation],
) -> int:
    present = {row.id: row for row in db.scalars(select(Conversation))}
    written = 0
    for batch in _batched(conversations.values()):
        for derived in batch:
            row = derived.row
            expected = {
                "id": row.id,
                "tenant_id": tenant_id,
                "channel": row.channel_type,
                "account_scope": derived.account_scope,
                # A provider-threaded source row may legitimately have no
                # contact. Keep that absence as an empty non-null value rather
                # than inventing our own account as the external party.
                "contact": _conversation_contact(row) or "",
                "thread_key": derived.thread_key,
                "transport_thread_ref": row.external_thread_id,
                "status": row.status,
                "status_reason": None,
                "subject": row.subject,
                "tags": None,
                "first_message_at": row.first_message_at,
                "last_message_at": row.last_message_at,
                # Sub has no resolved_at column; the status is the fact and
                # inventing a timestamp would be worse than leaving it null.
                "resolved_at": None,
                "snoozed_until": row.snoozed_until,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            target = present.get(row.id)
            if target is not None:
                _require_exact(
                    target,
                    entity="mod_inbox.conversations",
                    entity_id=row.id,
                    expected=expected,
                )
                continue
            db.add(Conversation(**expected))
            written += 1
        db.flush()
    return written


def _write_messages(
    db: Session,
    tenant_id: uuid.UUID,
    messages: list[tuple[InboxMessage, _DerivedConversation, str]],
) -> int:
    present = {row.id: row for row in db.scalars(select(Message))}
    written = 0
    for batch in _batched(messages):
        for message, _parent, key in batch:
            expected = {
                "id": message.id,
                "tenant_id": tenant_id,
                "conversation_id": message.conversation_id,
                "channel": message.channel_type,
                "direction": message.direction,
                "message_key": key,
                "subject": message.subject,
                "body": message.body,
                "transport_message_ref": message.external_message_id,
                "transport_observation_ref": None,
                "author_id": None,
                "occurred_at": _occurred_at(message),
                "created_at": message.created_at,
                "updated_at": message.updated_at,
            }
            target = present.get(message.id)
            if target is not None:
                _require_exact(
                    target,
                    entity="mod_inbox.messages",
                    entity_id=message.id,
                    expected=expected,
                )
                continue
            db.add(Message(**expected))
            written += 1
        db.flush()
    return written


def _write_read_states(
    db: Session,
    tenant_id: uuid.UUID,
    conversations: dict[uuid.UUID, _DerivedConversation],
) -> int:
    present = {row.id: row for row in db.scalars(select(ConversationReadState))}
    written = 0
    rows = db.scalars(select(InboxConversationReadState))
    for batch in _batched(row for row in rows if row.conversation_id in conversations):
        for row in batch:
            expected = {
                "id": row.id,
                "tenant_id": tenant_id,
                "conversation_id": row.conversation_id,
                "actor_id": row.person_id,
                "last_read_message_id": row.last_read_message_id,
                "last_read_at": row.last_read_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            target = present.get(row.id)
            if target is not None:
                _require_exact(
                    target,
                    entity="mod_inbox.conversation_read_states",
                    entity_id=row.id,
                    expected=expected,
                )
                continue
            db.add(ConversationReadState(**expected))
            written += 1
        db.flush()
    return written


def _write_queues(
    db: Session, tenant_id: uuid.UUID
) -> tuple[dict[uuid.UUID, uuid.UUID], int]:
    """One queue per service team, bound through `inbox_queue_bindings`.

    Returns team id -> queue id, which every operational writer below needs.
    """
    bindings = {
        binding.service_team_id: binding
        for binding in db.scalars(select(InboxQueueBinding))
    }
    module_queues = {row.id: row for row in db.scalars(select(InboxQueue))}
    bound: dict[uuid.UUID, uuid.UUID] = {}
    written = 0
    for team in db.scalars(select(ServiceTeam)):
        code = f"{_QUEUE_CODE_PREFIX}{team.id}"
        binding = bindings.get(team.id)
        if binding is not None:
            queue = module_queues.get(binding.queue_id)
            if queue is None:
                raise BackfillDrift(
                    f"inbox queue binding for team {team.id} names missing queue "
                    f"{binding.queue_id}"
                )
            _require_exact(
                queue,
                entity="mod_inbox_ops.inbox_queues",
                entity_id=queue.id,
                expected={
                    "tenant_id": tenant_id,
                    "code": code,
                    "name": team.name,
                    "active": True,
                },
            )
            if binding.queue_code != code:
                raise BackfillDrift(
                    f"inbox queue binding for team {team.id} records code "
                    f"{binding.queue_code!r}, expected {code!r}"
                )
            bound[team.id] = queue.id
            continue
        from app.services.inbox_module.operations import create_queue

        queue = create_queue(db, code=code, name=team.name)
        db.add(
            InboxQueueBinding(
                service_team_id=team.id, queue_id=queue.id, queue_code=queue.code
            )
        )
        bound[team.id] = queue.id
        written += 1
    db.flush()
    return bound, written


def _write_presence(db: Session, tenant_id: uuid.UUID) -> int:
    """Presence, with Sub's own defaults for the two columns it allows to be null.

    `max_concurrent_conversations` is nullable in Sub and NOT NULL in the module.
    The fallback is `resolve_default_max_concurrent_conversations`, which is the
    settings-backed value the running assignment service already applies to a
    null — so the backfill reproduces current behaviour instead of inventing a
    capacity. Writing 0 would silently make every such agent unassignable.

    `last_seen_at` is nullable for an agent who has never connected; `updated_at`
    is when Sub last touched the row, which is the most recent moment the state
    is known to have held.
    """
    from app.services.team_inbox_assignment import (
        InboxAgentPresenceDetailConflict,
        import_agent_presence_detail,
        resolve_default_max_concurrent_conversations,
    )

    default_capacity = resolve_default_max_concurrent_conversations(db)
    present = {
        row.agent_reference: row for row in db.scalars(select(ModuleAgentPresence))
    }
    written = 0
    for row in db.scalars(select(InboxAgentPresence)):
        reference = agent_reference(row.person_id)
        observed_at = row.last_seen_at or row.updated_at
        effective_status = row.manual_override_status or row.status
        try:
            _, detail_created = import_agent_presence_detail(
                db,
                person_id=row.person_id,
                status=effective_status,
                observed_at=observed_at,
            )
        except InboxAgentPresenceDetailConflict as exc:
            raise BackfillDrift(str(exc)) from exc
        written += int(detail_created)

        expected = {
            "id": row.id,
            "tenant_id": tenant_id,
            "agent_reference": reference,
            "state": presence_state(effective_status),
            "assignment_capacity": (
                row.max_concurrent_conversations
                if row.max_concurrent_conversations is not None
                else default_capacity
            ),
            "observed_at": observed_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        target = present.get(reference)
        if target is not None:
            _require_exact(
                target,
                entity="mod_inbox_ops.inbox_agent_presence",
                entity_id=reference,
                expected=expected,
            )
            continue
        db.add(ModuleAgentPresence(**expected))
        written += 1
    db.flush()
    return written


def _write_assignments(
    db: Session, tenant_id: uuid.UUID, queue_by_team: dict[uuid.UUID, uuid.UUID]
) -> int:
    present = {row.id: row for row in db.scalars(select(ConversationAssignment))}
    written = 0
    for row in db.scalars(select(InboxConversationAssignment)):
        queue_id = queue_by_team.get(row.service_team_id)
        if queue_id is None:
            continue
        expected = {
            "id": row.id,
            "tenant_id": tenant_id,
            "conversation_reference": conversation_reference(row.conversation_id),
            "queue_id": queue_id,
            "agent_reference": agent_reference(row.person_id),
            # Sub carries both `is_active` and `ended_at`. The module has one
            # status, and `is_active` is the flag its dispatch equivalent reads.
            "status": (
                AssignmentStatus.ASSIGNED
                if row.is_active
                else AssignmentStatus.RELEASED
            ),
            "assigned_at": row.assigned_at,
            "released_at": row.ended_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        target = present.get(row.id)
        if target is not None:
            _require_exact(
                target,
                entity="mod_inbox_ops.conversation_assignments",
                entity_id=row.id,
                expected=expected,
            )
            continue
        db.add(ConversationAssignment(**expected))
        written += 1
    db.flush()
    return written


def _write_queue_entries(
    db: Session, tenant_id: uuid.UUID, queue_by_team: dict[uuid.UUID, uuid.UUID]
) -> int:
    present = {row.id: row for row in db.scalars(select(InboxQueueEntry))}
    written = 0
    for row in db.scalars(
        select(InboxConversationQueueEntry).order_by(
            InboxConversationQueueEntry.entered_at
        )
    ):
        queue_id = queue_by_team.get(row.service_team_id)
        if queue_id is None:
            continue
        expected = {
            "id": row.id,
            "tenant_id": tenant_id,
            "queue_id": queue_id,
            "conversation_reference": conversation_reference(row.conversation_id),
            "queue_position": row.queue_position,
            "status": QueueEntryStatus(row.status.upper()),
            "entered_at": row.entered_at,
            "settled_at": row.settled_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        target = present.get(row.id)
        if target is not None:
            _require_exact(
                target,
                entity="mod_inbox_ops.inbox_queue_entries",
                entity_id=row.id,
                expected=expected,
            )
            continue
        db.add(InboxQueueEntry(**expected))
        written += 1
    db.flush()
    return written


def _write_cursors(
    db: Session, tenant_id: uuid.UUID, queue_by_team: dict[uuid.UUID, uuid.UUID]
) -> int:
    present = {row.queue_id: row for row in db.scalars(select(InboxRoundRobinCursor))}
    written = 0
    for row in db.scalars(select(InboxTeamRoundRobinCursor)):
        queue_id = queue_by_team.get(row.service_team_id)
        if queue_id is None:
            continue
        expected = {
            "id": row.id,
            "tenant_id": tenant_id,
            "queue_id": queue_id,
            "last_assigned_agent_reference": (
                agent_reference(row.last_assigned_person_id)
                if row.last_assigned_person_id is not None
                else None
            ),
            "rotation_count": row.rotation_count,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        target = present.get(queue_id)
        if target is not None:
            _require_exact(
                target,
                entity="mod_inbox_ops.inbox_round_robin_cursors",
                entity_id=queue_id,
                expected=expected,
            )
            continue
        db.add(InboxRoundRobinCursor(**expected))
        written += 1
    db.flush()
    return written
