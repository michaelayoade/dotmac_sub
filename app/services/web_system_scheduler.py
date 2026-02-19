"""Service helpers for admin system scheduler pages."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.scheduler import ScheduledTask
from app.services import scheduler as scheduler_service


def get_scheduler_overview_data(
    db: Session,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Return paginated scheduled tasks plus totals."""
    offset = (page - 1) * per_page
    tasks = scheduler_service.scheduled_tasks.list(
        db=db,
        enabled=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    total = db.scalar(select(func.count()).select_from(ScheduledTask)) or 0
    total_pages = (total + per_page - 1) // per_page
    return {
        "tasks": tasks,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def get_scheduler_task_detail_data(db: Session, task_id: str) -> dict[str, object] | None:
    """Return task detail data including computed next run time."""
    task = scheduler_service.scheduled_tasks.get(db, task_id)
    if not task:
        return None
    next_run = None
    if task.enabled and task.last_run_at:
        next_run = task.last_run_at + timedelta(seconds=task.interval_seconds)
    return {
        "task": task,
        "next_run": next_run,
        "runs": [],
    }
