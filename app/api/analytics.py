from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.analytics import (
    KPIAggregateCreate,
    KPIAggregateRead,
    KPIConfigCreate,
    KPIConfigRead,
    KPIConfigUpdate,
    KPIReadout,
)
from app.schemas.common import ListResponse
from app.services import analytics as analytics_service
from app.services.response import list_response

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.post("/kpi-configs", response_model=KPIConfigRead, status_code=status.HTTP_201_CREATED)
def create_kpi_config(payload: KPIConfigCreate, db: Session = Depends(get_db)):
    return analytics_service.kpi_configs.create(db, payload)


@router.get("/kpi-configs/{config_id}", response_model=KPIConfigRead)
def get_kpi_config(config_id: str, db: Session = Depends(get_db)):
    return analytics_service.kpi_configs.get(db, config_id)


@router.get("/kpi-configs", response_model=ListResponse[KPIConfigRead])
def list_kpi_configs(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = analytics_service.kpi_configs.list(
        db, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch("/kpi-configs/{config_id}", response_model=KPIConfigRead)
def update_kpi_config(
    config_id: str, payload: KPIConfigUpdate, db: Session = Depends(get_db)
):
    return analytics_service.kpi_configs.update(db, config_id, payload)


@router.delete("/kpi-configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_kpi_config(config_id: str, db: Session = Depends(get_db)):
    analytics_service.kpi_configs.delete(db, config_id)


@router.post("/kpi-aggregates", response_model=KPIAggregateRead, status_code=status.HTTP_201_CREATED)
def create_kpi_aggregate(payload: KPIAggregateCreate, db: Session = Depends(get_db)):
    return analytics_service.kpi_aggregates.create(db, payload)


@router.get("/kpi-aggregates/{aggregate_id}", response_model=KPIAggregateRead)
def get_kpi_aggregate(aggregate_id: str, db: Session = Depends(get_db)):
    return analytics_service.kpi_aggregates.get(db, aggregate_id)


@router.get("/kpi-aggregates", response_model=ListResponse[KPIAggregateRead])
def list_kpi_aggregates(
    key: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = analytics_service.kpi_aggregates.list(
        db, key, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.get("/kpis", response_model=list[KPIReadout])
def compute_kpis(db: Session = Depends(get_db)):
    return analytics_service.compute_kpis(db)
