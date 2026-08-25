"""Sub's only door to `dotmac_inbox_operations`.

Every function here takes Sub identifiers and hands the module a `TenantScope`
plus one of its frozen command dataclasses. The module owns capacity,
eligibility enforcement, FIFO position, rotation and the routing decision
record; Sub owns who is eligible in the first place, which is a Workforce
question the module deliberately refuses to answer.

That split is the one thing to keep straight when reading this file: Sub PASSES
eligibility in (`eligible_agent_references`), and the module DECIDES with it.
Neither half works alone, and putting the eligibility query in here rather than
in the Workforce owner would quietly make this facade a second policy owner.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from dotmac_inbox_operations.contracts import (
    AdmitToQueue,
    AssignConversation,
    AssignmentStatus,
    Conflict,
    CreateQueue,
    CreateRoutingRule,
    DispatchQueues,
    PresenceState,
    PromoteFromQueue,
    QueueEligibility,
    QueueEntryStatus,
    ReleaseConversation,
    RouteConversation,
    RoutedConversation,
    SetAgentPresence,
)
from dotmac_inbox_operations.models import (
    ConversationAssignment,
    InboxQueue,
    InboxQueueEntry,
    InboxRoutingRule,
)
from dotmac_inbox_operations.service import (
    admit_to_queue as _admit_to_queue,
)
from dotmac_inbox_operations.service import (
    assign_conversation as _assign_conversation,
)
from dotmac_inbox_operations.service import (
    cancel_queue_entry as _cancel_queue_entry,
)
from dotmac_inbox_operations.service import (
    create_queue as _create_queue,
)
from dotmac_inbox_operations.service import (
    create_routing_rule as _create_routing_rule,
)
from dotmac_inbox_operations.service import (
    dispatch_queues_fairly as _dispatch_queues_fairly,
)
from dotmac_inbox_operations.service import (
    release_conversation as _release_conversation,
)
from dotmac_inbox_operations.service import (
    route_conversation as _route_conversation,
)
from dotmac_inbox_operations.service import (
    set_agent_presence as _set_agent_presence,
)
from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session

from app.services.inbox_module.references import (
    agent_reference,
    conversation_reference,
    presence_state,
)
from app.services.operator_tenant import operator_tenant_id

__all__ = [
    "AssignmentStatus",
    "Conflict",
    "ConversationAssignment",
    "InboxQueue",
    "InboxQueueEntry",
    "InboxRoutingRule",
    "PresenceState",
    "QueueEntryStatus",
    "RoutedConversation",
    "admit",
    "assign",
    "cancel",
    "create_queue",
    "create_routing_rule",
    "dispatch_fairly",
    "release",
    "route",
    "set_presence",
]


def _scope() -> TenantScope:
    """Sub is a dedicated single-operator deployment; ADR-0009 names the tenant."""
    return TenantScope(tenant_id=operator_tenant_id())


def create_queue(db: Session, *, code: str, name: str) -> InboxQueue:
    """Create a queue. Binding it to a service team is `inbox_queue_bindings`."""
    return _create_queue(db, scope=_scope(), command=CreateQueue(code=code, name=name))


def create_routing_rule(
    db: Session, *, queue_id: uuid.UUID, channel_code: str, priority: int
) -> InboxRoutingRule:
    """Declare that a channel routes to a queue at a priority."""
    return _create_routing_rule(
        db,
        scope=_scope(),
        command=CreateRoutingRule(
            queue_id=queue_id, channel_code=channel_code, priority=priority
        ),
    )


def route(
    db: Session,
    *,
    decision_reference: str,
    conversation_id: uuid.UUID,
    channel: str,
    routed_at: datetime,
) -> RoutedConversation:
    """Resolve the queue for a conversation and durably record why.

    `decision_reference` belongs to the ingress adapter and is what makes a
    replayed webhook idempotent — the module returns the original decision
    rather than routing twice. Sub passes the provider event id it already
    stores on `inbox_provider_observations`, never a freshly minted uuid, or the
    idempotency is decorative.
    """
    return _route_conversation(
        db,
        scope=_scope(),
        command=RouteConversation(
            decision_reference=decision_reference,
            conversation_reference=conversation_reference(conversation_id),
            channel_code=channel,
            routed_at=routed_at,
        ),
    )


def admit(
    db: Session,
    *,
    queue_id: uuid.UUID,
    conversation_id: uuid.UUID,
    entered_at: datetime | None = None,
) -> InboxQueueEntry:
    """Put a conversation at the back of a queue, idempotently."""
    return _admit_to_queue(
        db,
        scope=_scope(),
        command=AdmitToQueue(
            queue_id=queue_id,
            conversation_reference=conversation_reference(conversation_id),
            entered_at=entered_at,
        ),
    )


def cancel(db: Session, *, queue_entry_id: uuid.UUID) -> InboxQueueEntry:
    """Withdraw a queued conversation without assigning it.

    The module stamps `settled_at` itself; there is no caller-supplied time,
    because a cancellation is settled when it is recorded.
    """
    return _cancel_queue_entry(db, scope=_scope(), entry_id=queue_entry_id)


def set_presence(
    db: Session,
    *,
    person_id: uuid.UUID,
    status,
    assignment_capacity: int,
    observed_at: datetime,
):
    """Record one operator's presence and capacity.

    `status` is Sub's four-state `InboxAgentPresenceStatus`; the narrowing to
    the module's three states happens in `references.presence_state` and is
    recorded in ADR-0013 § 6.
    """
    return _set_agent_presence(
        db,
        scope=_scope(),
        command=SetAgentPresence(
            agent_reference=agent_reference(person_id),
            state=presence_state(status),
            assignment_capacity=assignment_capacity,
            observed_at=observed_at,
        ),
    )


def assign(
    db: Session,
    *,
    conversation_id: uuid.UUID,
    queue_id: uuid.UUID,
    person_id: uuid.UUID,
    assigned_at: datetime,
    eligible_person_ids: tuple[uuid.UUID, ...],
    presence_fresh_after: datetime,
) -> ConversationAssignment:
    """Assign a named operator, refused if capacity or eligibility says no.

    `eligible_person_ids` comes from the Workforce owner, not from here. The
    module checks membership and capacity under a lock; it does not know how
    Sub decided the set.
    """
    return _assign_conversation(
        db,
        scope=_scope(),
        command=AssignConversation(
            conversation_reference=conversation_reference(conversation_id),
            queue_id=queue_id,
            agent_reference=agent_reference(person_id),
            assigned_at=assigned_at,
            eligible_agent_references=tuple(
                agent_reference(candidate) for candidate in eligible_person_ids
            ),
            presence_fresh_after=presence_fresh_after,
        ),
    )


def release(
    db: Session, *, assignment_id: uuid.UUID, released_at: datetime, reason: str
) -> ConversationAssignment:
    """Hand work back. The module keeps the release as workflow evidence."""
    return _release_conversation(
        db,
        scope=_scope(),
        command=ReleaseConversation(
            assignment_id=assignment_id, released_at=released_at, reason=reason
        ),
    )


def dispatch_fairly(
    db: Session,
    *,
    eligibility_by_queue: dict[uuid.UUID, tuple[uuid.UUID, ...]],
    dispatched_at: datetime,
    presence_fresh_after: datetime,
) -> tuple[ConversationAssignment, ...]:
    """Attempt ONE promotion per queue, so a saturated queue cannot starve peers.

    This replaces Sub's global oldest-first sweep, which took the oldest limited
    batch across every team and let one busy team occupy the whole window. That
    was the fairness defect the audit named; the module's per-queue pass is the
    fix, and it is why the caller must pass eligibility per queue rather than
    one global agent list.
    """
    return _dispatch_queues_fairly(
        db,
        scope=_scope(),
        command=DispatchQueues(
            queues=tuple(
                QueueEligibility(
                    queue_id=queue_id,
                    agent_references=tuple(
                        agent_reference(person_id) for person_id in person_ids
                    ),
                )
                for queue_id, person_ids in eligibility_by_queue.items()
            ),
            dispatched_at=dispatched_at,
            presence_fresh_after=presence_fresh_after,
        ),
    )


def promote(
    db: Session,
    *,
    queue_id: uuid.UUID,
    eligible_person_ids: tuple[uuid.UUID, ...],
    presence_fresh_after: datetime,
    promoted_at: datetime | None = None,
) -> ConversationAssignment:
    """Promote the head of one queue onto an eligible available agent."""
    from dotmac_inbox_operations.service import promote_from_queue as _promote

    return _promote(
        db,
        scope=_scope(),
        command=PromoteFromQueue(
            queue_id=queue_id,
            eligible_agent_references=tuple(
                agent_reference(person_id) for person_id in eligible_person_ids
            ),
            presence_fresh_after=presence_fresh_after,
            promoted_at=promoted_at,
        ),
    )
