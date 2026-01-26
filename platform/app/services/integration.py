from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.connector import ConnectorConfig
from app.models.integration import (
    IntegrationJob,
    IntegrationJobType,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTarget,
    IntegrationTargetType,
)
from app.schemas.integration import (
    IntegrationJobCreate,
    IntegrationJobUpdate,
    IntegrationTargetCreate,
    IntegrationTargetUpdate,
)


def _apply_ordering(query, order_by, order_dir, allowed_columns):
    if order_by not in allowed_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order_by. Allowed: {', '.join(sorted(allowed_columns))}",
        )
    column = allowed_columns[order_by]
    if order_dir == "desc":
        return query.order_by(column.desc())
    return query.order_by(column.asc())


def _apply_pagination(query, limit, offset):
    return query.limit(limit).offset(offset)


def _validate_enum(value, enum_cls, label):
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc


class IntegrationTargets:
    @staticmethod
    def create(db: Session, payload: IntegrationTargetCreate):
        if payload.connector_config_id:
            config = db.get(ConnectorConfig, payload.connector_config_id)
            if not config:
                raise HTTPException(status_code=404, detail="Connector config not found")
        target = IntegrationTarget(**payload.model_dump())
        db.add(target)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def get(db: Session, target_id: str):
        target = db.get(IntegrationTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        return target

    @staticmethod
    def list(
        db: Session,
        target_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationTarget)
        if target_type:
            query = query.filter(
                IntegrationTarget.target_type
                == _validate_enum(target_type, IntegrationTargetType, "target_type")
            )
        if is_active is None:
            query = query.filter(IntegrationTarget.is_active.is_(True))
        else:
            query = query.filter(IntegrationTarget.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationTarget.created_at, "name": IntegrationTarget.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, target_id: str, payload: IntegrationTargetUpdate):
        target = db.get(IntegrationTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        data = payload.model_dump(exclude_unset=True)
        if "connector_config_id" in data and data["connector_config_id"]:
            config = db.get(ConnectorConfig, data["connector_config_id"])
            if not config:
                raise HTTPException(status_code=404, detail="Connector config not found")
        for key, value in data.items():
            setattr(target, key, value)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def delete(db: Session, target_id: str):
        target = db.get(IntegrationTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        target.is_active = False
        db.commit()


class IntegrationJobs:
    @staticmethod
    def create(db: Session, payload: IntegrationJobCreate):
        target = db.get(IntegrationTarget, payload.target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        job = IntegrationJob(**payload.model_dump())
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def get(db: Session, job_id: str):
        job = db.get(IntegrationJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        return job

    @staticmethod
    def list(
        db: Session,
        target_id: str | None,
        job_type: str | None,
        schedule_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationJob)
        if target_id:
            query = query.filter(IntegrationJob.target_id == target_id)
        if job_type:
            query = query.filter(
                IntegrationJob.job_type
                == _validate_enum(job_type, IntegrationJobType, "job_type")
            )
        if schedule_type:
            query = query.filter(
                IntegrationJob.schedule_type
                == _validate_enum(schedule_type, IntegrationScheduleType, "schedule_type")
            )
        if is_active is None:
            query = query.filter(IntegrationJob.is_active.is_(True))
        else:
            query = query.filter(IntegrationJob.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationJob.created_at, "name": IntegrationJob.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, job_id: str, payload: IntegrationJobUpdate):
        job = db.get(IntegrationJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        data = payload.model_dump(exclude_unset=True)
        if "target_id" in data:
            target = db.get(IntegrationTarget, data["target_id"])
            if not target:
                raise HTTPException(status_code=404, detail="Integration target not found")
        for key, value in data.items():
            setattr(job, key, value)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def delete(db: Session, job_id: str):
        job = db.get(IntegrationJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        job.is_active = False
        db.commit()

    @staticmethod
    def run(db: Session, job_id: str):
        job = db.get(IntegrationJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        run = IntegrationRun(job_id=job.id, status=IntegrationRunStatus.running)
        db.add(run)
        db.commit()
        db.refresh(run)
        run.status = IntegrationRunStatus.success
        run.finished_at = datetime.utcnow()
        job.last_run_at = run.finished_at
        db.commit()
        db.refresh(run)
        return run


class IntegrationRuns:
    @staticmethod
    def list(
        db: Session,
        job_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationRun)
        if job_id:
            query = query.filter(IntegrationRun.job_id == job_id)
        if status:
            query = query.filter(
                IntegrationRun.status
                == _validate_enum(status, IntegrationRunStatus, "status")
            )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationRun.created_at, "status": IntegrationRun.status},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(IntegrationRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Integration run not found")
        return run


integration_targets = IntegrationTargets()
integration_jobs = IntegrationJobs()
integration_runs = IntegrationRuns()


def list_interval_jobs(db: Session) -> list[IntegrationJob]:
    return (
        db.query(IntegrationJob)
        .filter(IntegrationJob.is_active.is_(True))
        .filter(IntegrationJob.schedule_type == IntegrationScheduleType.interval)
        .filter(IntegrationJob.interval_minutes.isnot(None))
        .all()
    )
