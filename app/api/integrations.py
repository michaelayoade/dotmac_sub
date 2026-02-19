from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.integration import (
    IntegrationJobCreate,
    IntegrationJobRead,
    IntegrationJobUpdate,
    IntegrationRunRead,
    IntegrationTargetCreate,
    IntegrationTargetRead,
    IntegrationTargetUpdate,
)
from app.services import integration as integration_service

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.post(
    "/targets",
    response_model=IntegrationTargetRead,
    status_code=status.HTTP_201_CREATED,
)
def create_integration_target(
    payload: IntegrationTargetCreate, db: Session = Depends(get_db)
):
    return integration_service.integration_targets.create(db, payload)


@router.get("/targets/{target_id}", response_model=IntegrationTargetRead)
def get_integration_target(target_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_targets.get(db, target_id)


@router.get("/targets", response_model=ListResponse[IntegrationTargetRead])
def list_integration_targets(
    target_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return integration_service.integration_targets.list_response(
        db, target_type, is_active, order_by, order_dir, limit, offset
    )


@router.patch("/targets/{target_id}", response_model=IntegrationTargetRead)
def update_integration_target(
    target_id: str, payload: IntegrationTargetUpdate, db: Session = Depends(get_db)
):
    return integration_service.integration_targets.update(db, target_id, payload)


@router.delete("/targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration_target(target_id: str, db: Session = Depends(get_db)):
    integration_service.integration_targets.delete(db, target_id)


@router.post(
    "/jobs",
    response_model=IntegrationJobRead,
    status_code=status.HTTP_201_CREATED,
)
def create_integration_job(payload: IntegrationJobCreate, db: Session = Depends(get_db)):
    return integration_service.integration_jobs.create(db, payload)


@router.get("/jobs/{job_id}", response_model=IntegrationJobRead)
def get_integration_job(job_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_jobs.get(db, job_id)


@router.get("/jobs", response_model=ListResponse[IntegrationJobRead])
def list_integration_jobs(
    target_id: str | None = None,
    job_type: str | None = None,
    schedule_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return integration_service.integration_jobs.list_response(
        db, target_id, job_type, schedule_type, is_active, order_by, order_dir, limit, offset
    )


@router.patch("/jobs/{job_id}", response_model=IntegrationJobRead)
def update_integration_job(
    job_id: str, payload: IntegrationJobUpdate, db: Session = Depends(get_db)
):
    return integration_service.integration_jobs.update(db, job_id, payload)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration_job(job_id: str, db: Session = Depends(get_db)):
    integration_service.integration_jobs.delete(db, job_id)


@router.post("/jobs/{job_id}/run", response_model=IntegrationRunRead)
def run_integration_job(job_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_jobs.run(db, job_id)


@router.post("/jobs/refresh-schedule", status_code=status.HTTP_200_OK)
def refresh_integration_schedule(db: Session = Depends(get_db)):
    return integration_service.refresh_schedule(db)


@router.get("/runs/{run_id}", response_model=IntegrationRunRead)
def get_integration_run(run_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_runs.get(db, run_id)


@router.get("/runs", response_model=ListResponse[IntegrationRunRead])
def list_integration_runs(
    job_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return integration_service.integration_runs.list_response(
        db, job_id, status, order_by, order_dir, limit, offset
    )
