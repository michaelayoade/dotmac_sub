"""Provisioning convergence triage — one attention queue over the provisioning owners.

Merges the attention-worthy provisioning work into a single worst-first queue:
  - runs (owner: provisioning_runs) mapped to the canonical control-plane phase
    via control_plane_intent.phase_for_provisioning_run
  - open service orders (owner: service_orders)
  - open tasks (owner: provisioning_tasks)

Runs carry the ControlPlanePhase convergence lens; orders/tasks carry their own
lifecycle status. Ordering is worst-first by the server-owned tone (the NOC
pattern). Read-only projection; each row links to its facet in the provisioning
ledger. Phase mapping stays owner-side (control_plane_intent), not here.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.schemas.status_presentation import StatusTone
from app.services.control_plane_intent import phase_for_provisioning_run
from app.services.provisioning_managers import (
    provisioning_runs,
    provisioning_tasks,
    service_orders,
)
from app.services.status_presentation import (
    control_plane_phase_presentation,
    provisioning_task_status_presentation,
    service_order_status_presentation,
)

_LIMIT = 500

# worst-first ordering for the queue (same ranking as the NOC triage)
_TONE_RANK = {
    StatusTone.negative: 0,
    StatusTone.warning: 1,
    StatusTone.info: 2,
    StatusTone.positive: 3,
    StatusTone.neutral: 4,
}

# attention = non-terminal + failed, per owner (terminal-ok states are excluded:
# runs success, orders active/canceled/draft, tasks completed)
_RUN_ATTENTION = ("failed", "running", "pending")
_ORDER_ATTENTION = ("failed", "provisioning", "scheduled", "submitted")
_TASK_ATTENTION = ("failed", "blocked", "in_progress", "pending")


def _rank(presentation) -> int:
    return _TONE_RANK.get(presentation.tone, 5)


def _when(value: object) -> tuple:
    # (display string, sort epoch) — pre-formatted so the template needs no filter
    if not value:
        return "—", 0.0
    return value.strftime("%b %d, %H:%M"), value.timestamp()


def provisioning_triage_data(db: Session) -> dict:
    """Merge open runs / orders / tasks into one worst-first attention queue."""
    items: list[dict] = []
    run_count = order_count = task_count = 0

    # 1. Provisioning runs — the convergence unit, labelled with the control-plane phase
    for status in _RUN_ATTENTION:
        for r in provisioning_runs.list(db, None, status, "created_at", "desc", _LIMIT, 0):
            presentation = control_plane_phase_presentation(
                phase_for_provisioning_run(r.status)
            )
            when_label, when_epoch = _when(r.started_at or r.created_at)
            items.append(
                {
                    "kind": "run",
                    "id": str(r.id),
                    "title": f"Run {str(r.id)[:8]}",
                    "subtitle": (r.error_message or "")[:80] or None,
                    "status": presentation,
                    "when": when_label,
                    "url": "/admin/network/provisioning?facet=runs",
                    "_rank": _rank(presentation),
                    "_when": when_epoch,
                }
            )
            run_count += 1

    # 2. Open service orders
    for status in _ORDER_ATTENTION:
        for o in service_orders.list(db, status=status, limit=_LIMIT):
            presentation = service_order_status_presentation(o.status)
            when_label, when_epoch = _when(o.created_at)
            items.append(
                {
                    "kind": "order",
                    "id": str(o.id),
                    "title": f"Order {str(o.id)[:8]}",
                    "subtitle": getattr(o.order_type, "value", None),
                    "status": presentation,
                    "when": when_label,
                    "url": "/admin/network/provisioning?facet=orders",
                    "_rank": _rank(presentation),
                    "_when": when_epoch,
                }
            )
            order_count += 1

    # 3. Open tasks
    for status in _TASK_ATTENTION:
        for t in provisioning_tasks.list(db, None, status, "created_at", "desc", _LIMIT, 0):
            presentation = provisioning_task_status_presentation(t.status)
            when_label, when_epoch = _when(t.created_at)
            items.append(
                {
                    "kind": "task",
                    "id": str(t.id),
                    "title": t.name,
                    "subtitle": f"assigned {t.assigned_to}" if t.assigned_to else None,
                    "status": presentation,
                    "when": when_label,
                    "url": "/admin/network/provisioning?facet=tasks",
                    "_rank": _rank(presentation),
                    "_when": when_epoch,
                }
            )
            task_count += 1

    # worst tone first, then most recent
    items.sort(key=lambda i: (i["_rank"], -i["_when"]))

    return {
        "items": items,
        "counts": {
            "total": len(items),
            "runs": run_count,
            "orders": order_count,
            "tasks": task_count,
        },
    }
