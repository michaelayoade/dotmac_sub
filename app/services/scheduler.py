from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.scheduler import ScheduledTask, ScheduleType
from app.schemas.scheduler import ScheduledTaskCreate, ScheduledTaskUpdate
from app.services.common import (
    apply_ordering,
    apply_pagination,
)
from app.services.response import ListResponseMixin


def _validate_schedule_type(value):
    if value is None:
        return None
    if isinstance(value, ScheduleType):
        return value
    try:
        return ScheduleType(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid schedule_type") from exc


class ScheduledTasks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ScheduledTaskCreate):
        if payload.interval_seconds < 1:
            raise HTTPException(status_code=400, detail="interval_seconds must be >= 1")
        task = ScheduledTask(**payload.model_dump())
        db.add(task)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def get(db: Session, task_id: str):
        task = db.get(ScheduledTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        return task

    @staticmethod
    def list(
        db: Session,
        enabled: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ScheduledTask)
        if enabled is not None:
            query = query.filter(ScheduledTask.enabled == enabled)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ScheduledTask.created_at, "name": ScheduledTask.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, task_id: str, payload: ScheduledTaskUpdate):
        task = db.get(ScheduledTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        data = payload.model_dump(exclude_unset=True)
        if "schedule_type" in data:
            data["schedule_type"] = _validate_schedule_type(data["schedule_type"])
        if "interval_seconds" in data and data["interval_seconds"] is not None:
            if data["interval_seconds"] < 1:
                raise HTTPException(
                    status_code=400, detail="interval_seconds must be >= 1"
                )
        for key, value in data.items():
            setattr(task, key, value)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def delete(db: Session, task_id: str):
        task = db.get(ScheduledTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        db.delete(task)
        db.commit()


scheduled_tasks = ScheduledTasks()


def refresh_schedule() -> dict:
    return {"detail": "Celery beat refreshes schedules automatically."}


def enqueue_task(task_name: str, args: list | None, kwargs: dict | None) -> dict:
    from app.celery_app import celery_app

    async_result = celery_app.send_task(task_name, args=args or [], kwargs=kwargs or {})
    return {"queued": True, "task_id": str(async_result.id)}


def enqueue_by_id(db: Session, task_id: str) -> dict:
    """Look up a scheduled task and enqueue it."""
    task = scheduled_tasks.get(db, task_id)
    return enqueue_task(task.task_name, task.args_json or [], task.kwargs_json or {})
