"""Realtime workqueue updates.

Reuses sub's existing WebSocket transport (``app.websocket.manager``): a Redis
pub/sub fan-out whose routing key is a *topic* (team-inbox happens to use
conversation ids as topics; the workqueue uses ``workqueue:*`` channels). No new
transport, no new infrastructure.

Channels, mirroring CRM:
* ``workqueue:user:{person_id}``           — items assigned to (or snoozed by) one person
* ``workqueue:audience:team:{team_id}``    — items belonging to one service team
* ``workqueue:audience:org``               — everything, for org-audience viewers
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.types import ItemKind, WorkqueueAudience

logger = logging.getLogger(__name__)

ChangeKind = Literal["added", "removed", "updated"]

ORG_CHANNEL = "workqueue:audience:org"


def user_channel(user_id: UUID | str) -> str:
    return f"workqueue:user:{user_id}"


def team_channel(team_id: UUID | str) -> str:
    return f"workqueue:audience:team:{team_id}"


def org_channel() -> str:
    return ORG_CHANNEL


def channels_for_scope(scope: WorkqueueScope) -> list[str]:
    """The channels a connected viewer should be subscribed to."""
    channels = [user_channel(scope.person_id)]
    if scope.audience in (WorkqueueAudience.team, WorkqueueAudience.org):
        channels.extend(
            team_channel(team_id)
            for team_id in sorted(scope.accessible_service_team_ids, key=str)
        )
    if scope.audience is WorkqueueAudience.org:
        channels.append(org_channel())
    return channels


def _publish(channel: str, payload: dict) -> None:
    """Publish one event on the existing inbox WebSocket transport."""
    from app.websocket.events import EventType
    from app.websocket.realtime import publish_topic_event

    publish_topic_event(
        channel, event_type=EventType.WORKQUEUE_CHANGED, payload=payload
    )


def emit_item_change(
    *,
    item_kind: ItemKind | str,
    item_id: UUID,
    change: ChangeKind,
    assignee_id: UUID | None = None,
    previous_assignee_id: UUID | None = None,
    service_team_id: UUID | None = None,
    score: int | None = None,
    reason: str | None = None,
) -> None:
    """Emit for one domain record, deriving its channels from ownership.

    Both the new and the previous assignee are notified: a reassignment removes
    the item from one personal queue and adds it to another.
    """
    users = {
        person_id
        for person_id in (assignee_id, previous_assignee_id)
        if person_id is not None
    }
    teams = {service_team_id} if service_team_id is not None else set()
    emit_change(
        item_kind=item_kind,
        item_id=item_id,
        change=change,
        affected_user_ids=users,
        affected_team_ids=teams,
        affected_org=True,
        score=score,
        reason=reason,
    )


def emit_change(
    *,
    item_kind: ItemKind | str,
    item_id: UUID,
    change: ChangeKind,
    affected_user_ids: Iterable[UUID] = (),
    affected_team_ids: Iterable[UUID] = (),
    affected_org: bool = False,
    score: int | None = None,
    reason: str | None = None,
) -> None:
    """Tell every affected channel that one item entered/left/changed a queue.

    Best-effort by design: a realtime failure must never fail the write that
    triggered it (the queue is re-derivable on the next poll/refresh).
    """
    kind_label = item_kind.value if isinstance(item_kind, ItemKind) else str(item_kind)
    payload = {
        "type": "workqueue.changed",
        "item_kind": kind_label,
        "item_id": str(item_id),
        "change": change,
        "score": score,
        "reason": reason,
        "happened_at": datetime.now(UTC).isoformat(),
    }

    channels: list[str] = [user_channel(user_id) for user_id in affected_user_ids]
    channels.extend(team_channel(team_id) for team_id in affected_team_ids)
    if affected_org:
        channels.append(org_channel())

    for channel in channels:
        try:
            _publish(channel, payload)
        except Exception as exc:  # pragma: no cover — transport is best-effort
            logger.warning(
                "workqueue_emit_failed channel=%s item=%s error=%s",
                channel,
                item_id,
                exc,
            )
