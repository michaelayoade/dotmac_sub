"""Tax rate management service."""

import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import TaxRate
from app.schemas.billing import TaxRateCreate, TaxRateUpdate
from app.services.common import apply_ordering, apply_pagination, get_by_id
from app.services.response import ListResponseMixin
from app.services.sync_feeds import apply_sync_page, sync_page_response

logger = logging.getLogger(__name__)


class TaxRates(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: TaxRateCreate):
        rate = TaxRate(**payload.model_dump())
        db.add(rate)
        db.commit()
        db.refresh(rate)
        return rate

    @staticmethod
    def get(db: Session, rate_id: str):
        rate = get_by_id(db, TaxRate, rate_id)
        if not rate:
            raise HTTPException(status_code=404, detail="Tax rate not found")
        return rate

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(TaxRate)
        if is_active is None:
            stmt = stmt.where(TaxRate.is_active.is_(True))
        else:
            stmt = stmt.where(TaxRate.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {
                "created_at": TaxRate.created_at,
                "name": TaxRate.name,
                "rate": TaxRate.rate,
            },
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def list_for_sync(
        db: Session,
        *,
        is_active: bool | None,
        updated_since: datetime | None,
        limit: int,
        offset: int,
    ):
        query = db.query(TaxRate)
        if is_active is not None:
            query = query.filter(TaxRate.is_active == is_active)
        return apply_sync_page(
            query,
            TaxRate,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ).all()

    @classmethod
    def sync_list_response(cls, db: Session, **kwargs):
        items = cls.list_for_sync(db, **kwargs)
        return sync_page_response(items, limit=kwargs["limit"], offset=kwargs["offset"])

    @staticmethod
    def update(db: Session, rate_id: str, payload: TaxRateUpdate):
        rate = get_by_id(db, TaxRate, rate_id)
        if not rate:
            raise HTTPException(status_code=404, detail="Tax rate not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(rate, key, value)
        db.commit()
        db.refresh(rate)
        return rate

    @staticmethod
    def toggle_active(db: Session, rate_id: str) -> TaxRate:
        """Toggle a tax rate's active status."""
        rate = get_by_id(db, TaxRate, rate_id)
        if not rate:
            raise HTTPException(status_code=404, detail="Tax rate not found")
        rate.is_active = not rate.is_active
        db.commit()
        db.refresh(rate)
        logger.info(
            "Tax rate %s (%s) toggled to %s", rate.name, rate.id, rate.is_active
        )
        return rate

    @staticmethod
    def delete(db: Session, rate_id: str):
        rate = get_by_id(db, TaxRate, rate_id)
        if not rate:
            raise HTTPException(status_code=404, detail="Tax rate not found")
        rate.is_active = False
        db.commit()
