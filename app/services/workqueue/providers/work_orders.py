"""Work-order mirror provider.

The mirror is CRM-owned (durable-mirror pattern): its assignee is a *CRM* person
id and it carries no sub service team, so it cannot be scoped by team membership
or attributed to a sub person. Consequences, deliberately:

* ``self`` audience shows only *unclaimed* work orders (nothing links a mirrored
  work order to a sub person, so "mine" is not expressible).
* A ``service_team_id`` filter excludes work orders entirely rather than
  silently returning unfiltered rows.
* ``claim``/``complete`` are not offered — the record is not sub's to mutate.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.work_order_mirror import WorkOrderMirror
from app.services.workqueue.providers import register
from app.services.workqueue.providers.common import (
    as_utc,
    legacy_priority,
    score_item,
    seconds_until,
)
from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.scoring_config import WorkqueueScoringConfig
from app.services.workqueue.types import ActionKind, ItemKind, WorkqueueItem

CLOSED_WORK_ORDER_STATUSES = ("completed", "canceled")
IN_PROGRESS_STATUSES = frozenset({"in_progress", "started", "en_route", "paused"})


class WorkOrderProvider:
    kind = ItemKind.work_order

    def fetch(
        self,
        db: Session,
        *,
        scope: WorkqueueScope,
        config: WorkqueueScoringConfig,
        snoozed_ids: set[UUID],
        now: datetime,
        limit: int,
    ) -> list[WorkqueueItem]:
        if scope.service_team_filter is not None:
            # Mirrored work orders carry no sub service team — a team filter can
            # only be honoured by returning nothing.
            return []

        query = (
            db.query(WorkOrderMirror)
            .filter(WorkOrderMirror.is_active.is_(True))
            .filter(WorkOrderMirror.status.notin_(CLOSED_WORK_ORDER_STATUSES))
        )

        if scope.is_self_audience:
            query = query.filter(WorkOrderMirror.assigned_to_crm_person_id.is_(None))

        if snoozed_ids:
            query = query.filter(WorkOrderMirror.id.notin_(snoozed_ids))

        rows = (
            query.order_by(
                WorkOrderMirror.scheduled_start.asc().nullslast(),
                WorkOrderMirror.updated_at.desc(),
            )
            .limit(limit)
            .all()
        )
        return [self._to_item(work_order, config, now, scope) for work_order in rows]

    def _to_item(
        self,
        work_order: WorkOrderMirror,
        config: WorkqueueScoringConfig,
        now: datetime,
        scope: WorkqueueScope,
    ) -> WorkqueueItem:
        due_at = as_utc(work_order.scheduled_start)
        status = work_order.status

        candidates: list[tuple[int, str]] = [
            (config.work_order_scores["scheduled"], "scheduled")
        ]
        remaining = seconds_until(due_at, now)
        if remaining is not None:
            band = config.work_order_sla.band(remaining)
            if band is not None:
                reason, score = band
                candidates.append((score, reason))

        if str(work_order.priority or "").lower() == "urgent":
            candidates.append(
                (config.work_order_scores["priority_urgent"], "priority_urgent")
            )
        if status in IN_PROGRESS_STATUSES:
            candidates.append((config.work_order_scores["in_progress"], "in_progress"))
        if work_order.assigned_to_crm_person_id is None:
            candidates.append((config.work_order_scores["unassigned"], "unassigned"))

        score, reason, urgency = score_item(candidates, config)
        last_activity = as_utc(work_order.updated_at) or as_utc(work_order.created_at)

        return WorkqueueItem(
            item_kind=ItemKind.work_order,
            item_id=work_order.id,
            title=work_order.title or "Work order",
            subtitle=work_order.assigned_to_name or work_order.technician_name,
            status=status,
            priority=legacy_priority(work_order.priority),
            score=score,
            reason=reason,
            urgency=urgency,
            happened_at=last_activity or now,
            due_at=due_at,
            last_activity_at=last_activity,
            subscriber_id=work_order.subscriber_id,
            service_team_id=None,
            assigned_person_id=None,
            url=f"/admin/work-orders/{work_order.id}",
            # Snooze only: the mirror is CRM's record, not sub's to mutate.
            actions=(ActionKind.open, ActionKind.snooze),
            metadata={
                "work_type": work_order.work_type,
                "crm_work_order_id": work_order.crm_work_order_id,
                "audience": scope.audience.value,
            },
        )


work_order_provider = register(WorkOrderProvider())
