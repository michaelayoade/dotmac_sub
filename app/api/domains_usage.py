from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.usage import (
    QuotaBucketCreate,
    QuotaBucketRead,
    QuotaBucketUpdate,
    RadiusAccountingSessionCreate,
    RadiusAccountingSessionRead,
    UsageChargePostBatchRequest,
    UsageChargePostBatchResponse,
    UsageChargePostRequest,
    UsageChargeRead,
    UsageRatingRunRead,
    UsageRatingRunRequest,
    UsageRatingRunResponse,
    UsageRecordCreate,
    UsageRecordRead,
)
from app.services import usage as usage_service
from app.services.auth_dependencies import require_permission

router = APIRouter()


@router.get(
    "/quota-buckets",
    response_model=ListResponse[QuotaBucketRead],
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def list_quota_buckets(
    subscription_id: str | None = None,
    order_by: str = Query(default="period_start"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.quota_buckets.list_response(
        db, subscription_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/quota-buckets",
    response_model=QuotaBucketRead,
    status_code=status.HTTP_201_CREATED,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def create_quota_bucket(payload: QuotaBucketCreate, db: Session = Depends(get_db)):
    return usage_service.quota_buckets.create(db, payload)


@router.get(
    "/quota-buckets/{bucket_id}",
    response_model=QuotaBucketRead,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def get_quota_bucket(bucket_id: str, db: Session = Depends(get_db)):
    return usage_service.quota_buckets.get(db, bucket_id)


@router.patch(
    "/quota-buckets/{bucket_id}",
    response_model=QuotaBucketRead,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def update_quota_bucket(
    bucket_id: str, payload: QuotaBucketUpdate, db: Session = Depends(get_db)
):
    return usage_service.quota_buckets.update(db, bucket_id, payload)


@router.get(
    "/radius-accounting-sessions",
    response_model=ListResponse[RadiusAccountingSessionRead],
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def list_radius_accounting_sessions(
    subscription_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="started_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.radius_accounting_sessions.list_response(
        db, subscription_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/radius-accounting-sessions",
    response_model=RadiusAccountingSessionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def create_radius_accounting_session(
    payload: RadiusAccountingSessionCreate, db: Session = Depends(get_db)
):
    return usage_service.radius_accounting_sessions.create(db, payload)


@router.get(
    "/radius-accounting-sessions/{session_id}",
    response_model=RadiusAccountingSessionRead,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def get_radius_accounting_session(session_id: str, db: Session = Depends(get_db)):
    return usage_service.radius_accounting_sessions.get(db, session_id)


@router.get(
    "/usage-records",
    response_model=ListResponse[UsageRecordRead],
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def list_usage_records(
    subscription_id: str | None = None,
    order_by: str = Query(default="period_start"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.usage_records.list_response(
        db, subscription_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/usage-records",
    response_model=UsageRecordRead,
    status_code=status.HTTP_201_CREATED,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def create_usage_record(payload: UsageRecordCreate, db: Session = Depends(get_db)):
    return usage_service.usage_records.create(db, payload)


@router.get(
    "/usage-records/{record_id}",
    response_model=UsageRecordRead,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def get_usage_record(record_id: str, db: Session = Depends(get_db)):
    return usage_service.usage_records.get(db, record_id)


@router.get(
    "/usage-charges",
    response_model=ListResponse[UsageChargeRead],
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def list_usage_charges(
    subscription_id: str | None = None,
    is_posted: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.usage_charges.list_response(
        db, subscription_id, is_posted, order_by, order_dir, limit, offset
    )


@router.get(
    "/usage-charges/{charge_id}",
    response_model=UsageChargeRead,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def get_usage_charge(charge_id: str, db: Session = Depends(get_db)):
    return usage_service.usage_charges.get(db, charge_id)


@router.post(
    "/usage-charges/{charge_id}/post",
    response_model=UsageChargeRead,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def post_usage_charge(
    charge_id: str, payload: UsageChargePostRequest, db: Session = Depends(get_db)
):
    return usage_service.usage_charges.post_charge(db, charge_id, payload)


@router.post(
    "/usage-charges/post-batch",
    response_model=UsageChargePostBatchResponse,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def post_usage_charges_batch(
    payload: UsageChargePostBatchRequest, db: Session = Depends(get_db)
):
    return usage_service.usage_charges.post_batch(db, payload)


@router.post(
    "/usage-rating-runs",
    response_model=UsageRatingRunResponse,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def run_usage_rating(payload: UsageRatingRunRequest, db: Session = Depends(get_db)):
    return usage_service.usage_ratings.run(db, payload)


@router.get(
    "/usage-rating-runs",
    response_model=ListResponse[UsageRatingRunRead],
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def list_usage_rating_runs(
    started_by: str | None = None,
    order_by: str = Query(default="started_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.usage_ratings.list_runs_response(
        db, started_by, order_by, order_dir, limit, offset
    )


@router.get(
    "/usage-rating-runs/{run_id}",
    response_model=UsageRatingRunRead,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:read"))],
)
def get_usage_rating_run(run_id: str, db: Session = Depends(get_db)):
    return usage_service.usage_ratings.get_run(db, run_id)


@router.delete(
    "/quota-buckets/{bucket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def delete_quota_bucket(bucket_id: str, db: Session = Depends(get_db)):
    usage_service.quota_buckets.delete(db, bucket_id)


@router.delete(
    "/radius-accounting-sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def delete_radius_accounting_session(session_id: str, db: Session = Depends(get_db)):
    usage_service.radius_accounting_sessions.delete(db, session_id)


@router.delete(
    "/usage-records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["usage"],
    dependencies=[Depends(require_permission("usage:write"))],
)
def delete_usage_record(record_id: str, db: Session = Depends(get_db)):
    usage_service.usage_records.delete(db, record_id)
