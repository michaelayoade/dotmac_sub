"""The one writer of Sub's projected inbox columns, and its read-only twin.

After the P5 switch (`docs/adr/0013-inbox-authority-cutover.md` § 3),
`public.inbox_conversations` and `public.inbox_messages` are CACHES of
`mod_inbox`. Their projected columns have exactly one writer — `reconcile()` in
this file — and their Sub-owned columns are untouched by it.

`compare()` is the same field map read-only. Keeping both here is deliberate: a
comparator that consults a second copy of the mapping can report zero drift
while projecting the wrong column, which is the failure a shadow phase exists to
catch and would instead conceal.

## Why a projection rather than repointing the readers

Fifty-one files read `InboxConversation`. Repointing them all is a rewrite, not
a cutover, and it would hold the authority move hostage to unrelated work. The
projection lets authority move now and the readers move later, at the price of
one scheduled job. That price is stated in the ADR's consequences, not hidden.

A stopped reconciler degrades a cache: reads go stale, writes stay correct.
That is the failure mode this shape was chosen for.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from dotmac_inbox.models import Conversation, Message
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import InboxConversation, InboxMessage

__all__ = [
    "CONVERSATION_PROJECTION",
    "DriftReport",
    "MESSAGE_PROJECTION",
    "Drift",
    "compare",
    "reconcile",
]

#: module attribute -> Sub column. Every pair is a fact the module owns and Sub
#: mirrors. Anything NOT here is Sub's own and the reconciler must never touch
#: it — `subscriber_id`, `primary_service_team_id`, `priority`, `is_muted`,
#: `is_active`, `metadata`, `continued_from_conversation_id`.
CONVERSATION_PROJECTION: Final[dict[str, str]] = {
    "channel": "channel_type",
    "status": "status",
    "subject": "subject",
    "contact": "contact_address",
    "transport_thread_ref": "external_thread_id",
    "first_message_at": "first_message_at",
    "last_message_at": "last_message_at",
    "snoozed_until": "snoozed_until",
}

#: The same for messages. `notification_id`, `external_thread_id`,
#: `from_address`, `to_addresses`, `cc_addresses` and `metadata` stay Sub's.
#:
#: `occurred_at` projects onto `received_at` deliberately and ONLY for inbound
#: messages: Sub's `received_at`/`sent_at` split is a direction-dependent fact
#: the module folded into one column, so unfolding it needs the direction. See
#: `_message_targets`.
MESSAGE_PROJECTION: Final[dict[str, str]] = {
    "channel": "channel_type",
    "direction": "direction",
    "subject": "subject",
    "body": "body",
    "transport_message_ref": "external_message_id",
}

_BATCH: Final[int] = 500


@dataclass(frozen=True, slots=True)
class Drift:
    """One projected column that disagrees with the module."""

    entity: str
    entity_id: uuid.UUID
    column: str
    module_value: object
    sub_value: object


@dataclass
class DriftReport:
    """What the comparator saw, in both directions, for both entities.

    `missing_projection` and `orphan_projection` carry `(entity, id)` rather
    than a bare id. An earlier version kept bare ids and compared only
    conversations in the orphan direction, so a message written straight into
    `public.inbox_messages` was invisible to BOTH the writer baseline and the
    comparator — it disagreed with nothing, because the module had never heard
    of it. Naming the entity is what makes the asymmetry impossible to
    reintroduce silently.
    """

    conversations_compared: int = 0
    messages_compared: int = 0
    #: In the module, absent from Sub. A projection that was never built.
    missing_projection: list[tuple[str, uuid.UUID]] = field(default_factory=list)
    #: In Sub, absent from the module. Before the switch this is a backfill gap;
    #: after it, a local writer that was never retired — the failure mode this
    #: whole cutover exists to prevent.
    orphan_projection: list[tuple[str, uuid.UUID]] = field(default_factory=list)
    drift: list[Drift] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """The P5 gate. Anything but True blocks the switch."""
        return not (self.missing_projection or self.orphan_projection or self.drift)

    def _by_entity(self, rows: list[tuple[str, uuid.UUID]]) -> str:
        counts: dict[str, int] = {}
        for entity, _ in rows:
            counts[entity] = counts.get(entity, 0) + 1
        return "/".join(f"{entity}={count}" for entity, count in sorted(counts.items()))

    def summary(self) -> str:
        missing = self._by_entity(self.missing_projection) or "0"
        orphan = self._by_entity(self.orphan_projection) or "0"
        return (
            f"conversations={self.conversations_compared} "
            f"messages={self.messages_compared} "
            f"missing={len(self.missing_projection)}({missing}) "
            f"orphan={len(self.orphan_projection)}({orphan}) "
            f"drift={len(self.drift)}"
        )


def _message_targets(module_row: Message) -> dict[str, object]:
    """Sub's two timestamp columns from the module's one.

    Sub records `received_at` for what arrived and `sent_at` for what we sent.
    The module has a single `occurred_at`, which is the right shape — but the
    projection has to put it back where the readers look, and which column that
    is depends on the direction.
    """
    if module_row.direction == "inbound":
        return {"received_at": module_row.occurred_at}
    return {"sent_at": module_row.occurred_at}


def _batched(rows, size: int = _BATCH) -> Iterator[list]:
    batch: list = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def reconcile(db: Session) -> int:
    """Rebuild every projected column from `mod_inbox`. Idempotent and total.

    Total means: emptying the projected columns and running this restores them
    exactly. Idempotent means: running it twice writes nothing the second time.
    Both are tested, because a reconciler that is only *usually* total is a
    reconciler nobody dares run after an incident — which is precisely when it
    is needed.

    Returns the number of rows changed. Zero is the healthy steady state.
    """
    changed = 0

    sub_conversations = {row.id: row for row in db.scalars(select(InboxConversation))}
    for batch in _batched(db.scalars(select(Conversation))):
        for module_row in batch:
            target = sub_conversations.get(module_row.id)
            if target is None:
                # A conversation the module has and Sub does not. The reconciler
                # does not create Sub rows: the Sub row carries Sub-owned
                # columns it has no way to invent. `compare()` reports it.
                continue
            if _apply(module_row, target, CONVERSATION_PROJECTION):
                changed += 1
        db.flush()

    sub_messages = {row.id: row for row in db.scalars(select(InboxMessage))}
    for message_batch in _batched(db.scalars(select(Message))):
        for module_message in message_batch:
            sub_message = sub_messages.get(module_message.id)
            if sub_message is None:
                continue
            touched = _apply(module_message, sub_message, MESSAGE_PROJECTION)
            for column, value in _message_targets(module_message).items():
                if getattr(sub_message, column) != value:
                    setattr(sub_message, column, value)
                    touched = True
            if touched:
                changed += 1
        db.flush()

    return changed


def _apply(module_row, target, projection: dict[str, str]) -> bool:
    touched = False
    for source_attr, target_column in projection.items():
        value = getattr(module_row, source_attr)
        if getattr(target, target_column) != value:
            setattr(target, target_column, value)
            touched = True
    return touched


def compare(db: Session) -> DriftReport:
    """Read-only drift between the module and its Sub projection.

    This is the P4 gate. It writes nothing, so it is safe to run against
    production on a schedule, and its `clean` property is the exact condition
    ADR-0013 names before the writer switch.
    """
    report = DriftReport()

    sub_conversations = {row.id: row for row in db.scalars(select(InboxConversation))}
    module_conversation_ids: set[uuid.UUID] = set()
    for module_row in db.scalars(select(Conversation)):
        report.conversations_compared += 1
        module_conversation_ids.add(module_row.id)
        target = sub_conversations.get(module_row.id)
        if target is None:
            report.missing_projection.append(("conversation", module_row.id))
            continue
        _collect(module_row, target, CONVERSATION_PROJECTION, "conversation", report)

    for sub_id in sub_conversations.keys() - module_conversation_ids:
        report.orphan_projection.append(("conversation", sub_id))

    sub_messages = {row.id: row for row in db.scalars(select(InboxMessage))}
    module_message_ids: set[uuid.UUID] = set()
    for module_message in db.scalars(select(Message)):
        report.messages_compared += 1
        module_message_ids.add(module_message.id)
        sub_message = sub_messages.get(module_message.id)
        if sub_message is None:
            report.missing_projection.append(("message", module_message.id))
            continue
        _collect(module_message, sub_message, MESSAGE_PROJECTION, "message", report)
        for column, value in _message_targets(module_message).items():
            actual = getattr(sub_message, column)
            if actual != value:
                report.drift.append(
                    Drift(
                        entity="message",
                        entity_id=module_message.id,
                        column=column,
                        module_value=value,
                        sub_value=actual,
                    )
                )

    # The symmetric half, and the one that was missing. A message written
    # straight into `public.inbox_messages` disagrees with nothing, because the
    # module never heard of it — so column comparison alone can report a clean
    # inbox while a local writer quietly keeps producing rows. Only counting
    # what Sub has and the module does not sees it.
    for sub_id in sub_messages.keys() - module_message_ids:
        report.orphan_projection.append(("message", sub_id))

    return report


def _collect(
    module_row, target, projection: dict[str, str], entity: str, report: DriftReport
) -> None:
    for source_attr, target_column in projection.items():
        expected = getattr(module_row, source_attr)
        actual = getattr(target, target_column)
        if expected != actual:
            report.drift.append(
                Drift(
                    entity=entity,
                    entity_id=module_row.id,
                    column=target_column,
                    module_value=expected,
                    sub_value=actual,
                )
            )
