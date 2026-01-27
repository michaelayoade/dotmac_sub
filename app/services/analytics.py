from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.analytics import KPIAggregate, KPIConfig
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.schemas.analytics import KPIAggregateCreate, KPIConfigCreate, KPIConfigUpdate
from app.services.response import ListResponseMixin


class KPIConfigs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: KPIConfigCreate):
        config = KPIConfig(**payload.model_dump())
        db.add(config)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def get(db: Session, config_id: str):
        config = db.get(KPIConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="KPI config not found")
        return config

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(KPIConfig)
        if is_active is None:
            query = query.filter(KPIConfig.is_active.is_(True))
        else:
            query = query.filter(KPIConfig.is_active == is_active)
        query = apply_ordering(
            query, order_by, order_dir, {"created_at": KPIConfig.created_at, "key": KPIConfig.key}
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, config_id: str, payload: KPIConfigUpdate):
        config = db.get(KPIConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="KPI config not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(config, key, value)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def delete(db: Session, config_id: str):
        config = db.get(KPIConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="KPI config not found")
        config.is_active = False
        db.commit()


class KPIAggregates(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: KPIAggregateCreate):
        aggregate = KPIAggregate(**payload.model_dump())
        db.add(aggregate)
        db.commit()
        db.refresh(aggregate)
        return aggregate

    @staticmethod
    def get(db: Session, aggregate_id: str):
        aggregate = db.get(KPIAggregate, aggregate_id)
        if not aggregate:
            raise HTTPException(status_code=404, detail="KPI aggregate not found")
        return aggregate

    @staticmethod
    def list(
        db: Session,
        key: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(KPIAggregate)
        if key:
            query = query.filter(KPIAggregate.key == key)
        query = apply_ordering(
            query, order_by, order_dir, {"created_at": KPIAggregate.created_at}
        )
        return apply_pagination(query, limit, offset).all()


def compute_kpis(db: Session) -> list[dict]:
    """Compute key performance indicators. CRM/ticket KPIs removed."""
    return []


kpi_configs = KPIConfigs()
kpi_aggregates = KPIAggregates()
