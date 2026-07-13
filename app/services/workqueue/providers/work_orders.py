"""Work-order mirror provider.

The mirror is CRM-owned (durable-mirror pattern). Its assignee is a CRM person
id, but TechnicianProfile is the canonical identity bridge to a Sub person;
native dispatch assignments provide the fallback for locally assigned work.
Consequences, deliberately:

* self/team audiences show only work attributable through that bridge.
* Unassigned or unmapped work is visible only at org audience. Without a
  Sub assignee or team, placing it in a narrower queue would widen access.
* ``claim``/``complete`` are not offered — the record is not sub's to mutate.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
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
        query = (
            db.query(WorkOrderMirror)
            .filter(WorkOrderMirror.is_active.is_(True))
            .filter(WorkOrderMirror.status.notin_(CLOSED_WORK_ORDER_STATUSES))
        )

        if not scope.is_org_wide or scope.service_team_filter is not None:
            query = query.filter(_visible_to_people(scope.accessible_person_ids))

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
        assigned_people = _assigned_people(db, rows)
        return [
            self._to_item(
                work_order,
                config,
                now,
                scope,
                assigned_person_id=assigned_people.get(work_order.id),
            )
            for work_order in rows
        ]

    def _to_item(
        self,
        work_order: WorkOrderMirror,
        config: WorkqueueScoringConfig,
        now: datetime,
        scope: WorkqueueScope,
        *,
        assigned_person_id: UUID | None,
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
            assigned_person_id=assigned_person_id,
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


def _visible_to_people(person_ids: frozenset[UUID]):
    """SQL predicate matching the field service assignee-resolution contract."""
    if not person_ids:
        return WorkOrderMirror.id.is_(None)

    direct_person = (
        select(TechnicianProfile.person_id)
        .where(TechnicianProfile.is_active.is_(True))
        .where(
            TechnicianProfile.crm_person_id == WorkOrderMirror.assigned_to_crm_person_id
        )
        .limit(1)
        .correlate(WorkOrderMirror)
        .scalar_subquery()
    )
    latest_technician = (
        select(WorkOrderAssignmentQueue.assigned_technician_id)
        .where(WorkOrderAssignmentQueue.work_order_mirror_id == WorkOrderMirror.id)
        .where(WorkOrderAssignmentQueue.assigned_technician_id.isnot(None))
        .order_by(WorkOrderAssignmentQueue.created_at.desc())
        .limit(1)
        .correlate(WorkOrderMirror)
        .scalar_subquery()
    )
    accessible_technicians = select(TechnicianProfile.id).where(
        TechnicianProfile.is_active.is_(True),
        TechnicianProfile.person_id.in_(person_ids),
    )
    return or_(
        direct_person.in_(person_ids),
        and_(
            direct_person.is_(None),
            latest_technician.in_(accessible_technicians),
        ),
    )


def _assigned_people(db: Session, rows: list[WorkOrderMirror]) -> dict[UUID, UUID]:
    """Resolve display assignees with the same direct-then-fallback rule."""
    if not rows:
        return {}

    crm_ids = {
        row.assigned_to_crm_person_id for row in rows if row.assigned_to_crm_person_id
    }
    profiles_by_crm = {
        profile.crm_person_id: profile.person_id
        for profile in (
            db.query(TechnicianProfile)
            .filter(TechnicianProfile.is_active.is_(True))
            .filter(TechnicianProfile.crm_person_id.in_(crm_ids))
            .all()
        )
        if profile.crm_person_id is not None
    }
    resolved = {
        row.id: profiles_by_crm[row.assigned_to_crm_person_id]
        for row in rows
        if row.assigned_to_crm_person_id in profiles_by_crm
    }

    unresolved_ids = [row.id for row in rows if row.id not in resolved]
    assignments = (
        db.query(WorkOrderAssignmentQueue)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id.in_(unresolved_ids))
        .filter(WorkOrderAssignmentQueue.assigned_technician_id.isnot(None))
        .order_by(
            WorkOrderAssignmentQueue.work_order_mirror_id,
            WorkOrderAssignmentQueue.created_at.desc(),
        )
        .all()
    )
    latest_by_work_order: dict[UUID, UUID] = {}
    for assignment in assignments:
        technician_id = assignment.assigned_technician_id
        if technician_id is None:
            continue
        latest_by_work_order.setdefault(
            assignment.work_order_mirror_id,
            technician_id,
        )

    people_by_technician = {
        profile.id: profile.person_id
        for profile in (
            db.query(TechnicianProfile)
            .filter(TechnicianProfile.is_active.is_(True))
            .filter(TechnicianProfile.id.in_(set(latest_by_work_order.values())))
            .all()
        )
    }
    for work_order_id, technician_id in latest_by_work_order.items():
        person_id = people_by_technician.get(technician_id)
        if person_id is not None:
            resolved[work_order_id] = person_id
    return resolved
