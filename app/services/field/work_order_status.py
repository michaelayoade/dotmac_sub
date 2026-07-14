"""Authoritative persisted work-order status vocabulary for field operations."""

from __future__ import annotations

from enum import StrEnum


class WorkOrderStatus(StrEnum):
    """Statuses persisted on ``WorkOrderMirror`` by CRM or native field flows."""

    draft = "draft"
    scheduled = "scheduled"
    dispatched = "dispatched"
    in_progress = "in_progress"
    paused = "paused"
    completed = "completed"
    canceled = "canceled"


WORK_ORDER_STATUSES = frozenset(status.value for status in WorkOrderStatus)
FIELD_OPEN_WORK_ORDER_STATUSES = frozenset(
    {
        WorkOrderStatus.scheduled.value,
        WorkOrderStatus.dispatched.value,
        WorkOrderStatus.in_progress.value,
        WorkOrderStatus.paused.value,
    }
)
ASSIGNABLE_WORK_ORDER_STATUSES = FIELD_OPEN_WORK_ORDER_STATUSES
TERMINAL_WORK_ORDER_STATUSES = frozenset(
    {WorkOrderStatus.completed.value, WorkOrderStatus.canceled.value}
)
# Read compatibility for legacy British spelling. Native writers use ``canceled``.
WORK_ORDER_TERMINAL_VALUES = TERMINAL_WORK_ORDER_STATUSES | {"cancelled"}
