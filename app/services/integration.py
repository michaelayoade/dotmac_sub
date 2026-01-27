from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.connector import ConnectorConfig, ConnectorType
from app.models.integration import (
    IntegrationJob,
    IntegrationJobType,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTarget,
    IntegrationTargetType,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.services.response import ListResponseMixin
from app.schemas.integration import (
    IntegrationJobCreate,
    IntegrationJobUpdate,
    IntegrationTargetCreate,
    IntegrationTargetUpdate,
)

logger = get_logger(__name__)


class IntegrationTargets(ListResponseMixin):
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
        target = db.get(IntegrationTarget, coerce_uuid(target_id))
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
                == validate_enum(target_type, IntegrationTargetType, "target_type")
            )
        if is_active is None:
            query = query.filter(IntegrationTarget.is_active.is_(True))
        else:
            query = query.filter(IntegrationTarget.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationTarget.created_at, "name": IntegrationTarget.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        target_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IntegrationTarget)
        if target_type:
            query = query.filter(
                IntegrationTarget.target_type
                == validate_enum(target_type, IntegrationTargetType, "target_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationTarget.created_at, "name": IntegrationTarget.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, target_id: str, payload: IntegrationTargetUpdate):
        target = db.get(IntegrationTarget, coerce_uuid(target_id))
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
        target = db.get(IntegrationTarget, coerce_uuid(target_id))
        if not target:
            raise HTTPException(status_code=404, detail="Integration target not found")
        target.is_active = False
        db.commit()


class IntegrationJobs(ListResponseMixin):
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
        job = db.get(IntegrationJob, coerce_uuid(job_id))
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
                == validate_enum(job_type, IntegrationJobType, "job_type")
            )
        if schedule_type:
            query = query.filter(
                IntegrationJob.schedule_type
                == validate_enum(schedule_type, IntegrationScheduleType, "schedule_type")
            )
        if is_active is None:
            query = query.filter(IntegrationJob.is_active.is_(True))
        else:
            query = query.filter(IntegrationJob.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationJob.created_at, "name": IntegrationJob.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        target_id: str | None,
        job_type: str | None,
        schedule_type: str | None,
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
                == validate_enum(job_type, IntegrationJobType, "job_type")
            )
        if schedule_type:
            query = query.filter(
                IntegrationJob.schedule_type
                == validate_enum(schedule_type, IntegrationScheduleType, "schedule_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IntegrationJob.created_at, "name": IntegrationJob.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, job_id: str, payload: IntegrationJobUpdate):
        job = db.get(IntegrationJob, coerce_uuid(job_id))
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
        job = db.get(IntegrationJob, coerce_uuid(job_id))
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        job.is_active = False
        db.commit()

    @staticmethod
    def run(db: Session, job_id: str):
        job = db.get(IntegrationJob, coerce_uuid(job_id))
        if not job:
            raise HTTPException(status_code=404, detail="Integration job not found")
        if not job.is_active:
            logger.info("EMAIL_POLL_EXIT reason=job_disabled job_id=%s", job_id)
        run = IntegrationRun(job_id=job.id, status=IntegrationRunStatus.running)
        db.add(run)
        db.commit()
        db.refresh(run)
        try:
            metrics = None
            # Email polling via CRM inbox removed
            run.status = IntegrationRunStatus.success
            run.metrics = metrics
        except Exception as exc:
            run.status = IntegrationRunStatus.failed
            run.error = str(exc)
            raise
        finally:
            run.finished_at = datetime.now(timezone.utc)
            job.last_run_at = run.finished_at
            db.commit()
            db.refresh(run)
        return run


class IntegrationRuns(ListResponseMixin):
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
                == validate_enum(status, IntegrationRunStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": IntegrationRun.created_at,
                "status": IntegrationRun.status,
                "started_at": IntegrationRun.started_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(IntegrationRun, coerce_uuid(run_id))
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
        .filter(
            (IntegrationJob.interval_seconds.isnot(None))
            | (IntegrationJob.interval_minutes.isnot(None))
        )
        .all()
    )


def refresh_schedule(db: Session) -> dict[str, object]:
    count = len(list_interval_jobs(db))
    return {
        "scheduled_jobs": count,
        "detail": "Celery beat loads schedules at startup. Restart beat to apply changes.",
    }
