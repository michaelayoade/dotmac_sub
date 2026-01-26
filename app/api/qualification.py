from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.common import ListResponse
from app.schemas.qualification import (
    BuildoutApproveRequest,
    BuildoutMilestoneCreate,
    BuildoutMilestoneRead,
    BuildoutMilestoneUpdate,
    BuildoutProjectCreate,
    BuildoutProjectRead,
    BuildoutProjectUpdate,
    BuildoutRequestCreate,
    BuildoutRequestRead,
    BuildoutRequestUpdate,
    BuildoutUpdateCreate,
    BuildoutUpdateListRead,
    CoverageAreaCreate,
    CoverageAreaRead,
    CoverageAreaUpdate,
    ServiceQualificationRead,
    ServiceQualificationRequest,
)
from app.services import qualification as qualification_service

router = APIRouter(prefix="/qualification", tags=["qualification"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/coverage-areas",
    response_model=CoverageAreaRead,
    status_code=status.HTTP_201_CREATED,
)
def create_coverage_area(
    payload: CoverageAreaCreate, db: Session = Depends(get_db)
):
    return qualification_service.coverage_areas.create(db, payload)


@router.get(
    "/coverage-areas/{area_id}",
    response_model=CoverageAreaRead,
)
def get_coverage_area(area_id: str, db: Session = Depends(get_db)):
    return qualification_service.coverage_areas.get(db, area_id)


@router.get(
    "/coverage-areas",
    response_model=ListResponse[CoverageAreaRead],
)
def list_coverage_areas(
    zone_key: str | None = None,
    buildout_status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return qualification_service.coverage_areas.list_response(
        db, zone_key, buildout_status, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/coverage-areas/{area_id}",
    response_model=CoverageAreaRead,
)
def update_coverage_area(
    area_id: str, payload: CoverageAreaUpdate, db: Session = Depends(get_db)
):
    return qualification_service.coverage_areas.update(db, area_id, payload)


@router.delete(
    "/coverage-areas/{area_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_coverage_area(area_id: str, db: Session = Depends(get_db)):
    qualification_service.coverage_areas.delete(db, area_id)


@router.post(
    "/check",
    response_model=ServiceQualificationRead,
    status_code=status.HTTP_201_CREATED,
)
def check_service_qualification(
    payload: ServiceQualificationRequest, db: Session = Depends(get_db)
):
    return qualification_service.service_qualifications.check(db, payload)


@router.get(
    "/checks/{qualification_id}",
    response_model=ServiceQualificationRead,
)
def get_service_qualification(
    qualification_id: str, db: Session = Depends(get_db)
):
    return qualification_service.service_qualifications.get(db, qualification_id)


@router.get(
    "/checks",
    response_model=ListResponse[ServiceQualificationRead],
)
def list_service_qualifications(
    status_filter: str | None = Query(default=None, alias="status"),
    coverage_area_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return qualification_service.service_qualifications.list_response(
        db, status_filter, coverage_area_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/buildout-requests",
    response_model=BuildoutRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_buildout_request(
    payload: BuildoutRequestCreate, db: Session = Depends(get_db)
):
    return qualification_service.buildout_requests.create(db, payload)


@router.get(
    "/buildout-requests/{request_id}",
    response_model=BuildoutRequestRead,
)
def get_buildout_request(request_id: str, db: Session = Depends(get_db)):
    return qualification_service.buildout_requests.get(db, request_id)


@router.get(
    "/buildout-requests",
    response_model=ListResponse[BuildoutRequestRead],
)
def list_buildout_requests(
    status_filter: str | None = Query(default=None, alias="status"),
    coverage_area_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return qualification_service.buildout_requests.list_response(
        db, status_filter, coverage_area_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/buildout-requests/{request_id}",
    response_model=BuildoutRequestRead,
)
def update_buildout_request(
    request_id: str, payload: BuildoutRequestUpdate, db: Session = Depends(get_db)
):
    return qualification_service.buildout_requests.update(db, request_id, payload)


@router.post(
    "/buildout-requests/{request_id}/approve",
    response_model=BuildoutProjectRead,
)
def approve_buildout_request(
    request_id: str, payload: BuildoutApproveRequest, db: Session = Depends(get_db)
):
    return qualification_service.buildout_requests.approve(db, request_id, payload)


@router.post(
    "/buildout-projects",
    response_model=BuildoutProjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_buildout_project(
    payload: BuildoutProjectCreate, db: Session = Depends(get_db)
):
    return qualification_service.buildout_projects.create(db, payload)


@router.get(
    "/buildout-projects/{project_id}",
    response_model=BuildoutProjectRead,
)
def get_buildout_project(project_id: str, db: Session = Depends(get_db)):
    return qualification_service.buildout_projects.get(db, project_id)


@router.get(
    "/buildout-projects",
    response_model=ListResponse[BuildoutProjectRead],
)
def list_buildout_projects(
    status_filter: str | None = Query(default=None, alias="status"),
    coverage_area_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return qualification_service.buildout_projects.list_response(
        db, status_filter, coverage_area_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/buildout-projects/{project_id}",
    response_model=BuildoutProjectRead,
)
def update_buildout_project(
    project_id: str, payload: BuildoutProjectUpdate, db: Session = Depends(get_db)
):
    return qualification_service.buildout_projects.update(db, project_id, payload)


@router.delete(
    "/buildout-projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_buildout_project(project_id: str, db: Session = Depends(get_db)):
    qualification_service.buildout_projects.delete(db, project_id)


@router.post(
    "/buildout-milestones",
    response_model=BuildoutMilestoneRead,
    status_code=status.HTTP_201_CREATED,
)
def create_buildout_milestone(
    payload: BuildoutMilestoneCreate, db: Session = Depends(get_db)
):
    return qualification_service.buildout_milestones.create(db, payload)


@router.get(
    "/buildout-milestones/{milestone_id}",
    response_model=BuildoutMilestoneRead,
)
def get_buildout_milestone(milestone_id: str, db: Session = Depends(get_db)):
    return qualification_service.buildout_milestones.get(db, milestone_id)


@router.get(
    "/buildout-milestones",
    response_model=ListResponse[BuildoutMilestoneRead],
)
def list_buildout_milestones(
    project_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="order_index"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return qualification_service.buildout_milestones.list_response(
        db, project_id, status, order_by, order_dir, limit, offset
    )


@router.patch(
    "/buildout-milestones/{milestone_id}",
    response_model=BuildoutMilestoneRead,
)
def update_buildout_milestone(
    milestone_id: str, payload: BuildoutMilestoneUpdate, db: Session = Depends(get_db)
):
    return qualification_service.buildout_milestones.update(db, milestone_id, payload)


@router.delete(
    "/buildout-milestones/{milestone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_buildout_milestone(milestone_id: str, db: Session = Depends(get_db)):
    qualification_service.buildout_milestones.delete(db, milestone_id)


@router.post(
    "/buildout-updates",
    response_model=BuildoutUpdateListRead,
    status_code=status.HTTP_201_CREATED,
)
def create_buildout_update(
    payload: BuildoutUpdateCreate, db: Session = Depends(get_db)
):
    return qualification_service.buildout_updates.create(db, payload)


@router.get(
    "/buildout-updates",
    response_model=ListResponse[BuildoutUpdateListRead],
)
def list_buildout_updates(
    project_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return qualification_service.buildout_updates.list_response(
        db, project_id, order_by, order_dir, limit, offset
    )