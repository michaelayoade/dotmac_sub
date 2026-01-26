"""Tax rate management service."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import TaxRate
from app.services.common import apply_ordering, apply_pagination, get_by_id
from app.services.response import ListResponseMixin
from app.schemas.billing import TaxRateCreate, TaxRateUpdate


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
        query = db.query(TaxRate)
        if is_active is None:
            query = query.filter(TaxRate.is_active.is_(True))
        else:
            query = query.filter(TaxRate.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": TaxRate.created_at,
                "name": TaxRate.name,
                "rate": TaxRate.rate,
            },
        )
        return apply_pagination(query, limit, offset).all()

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
    def delete(db: Session, rate_id: str):
        rate = get_by_id(db, TaxRate, rate_id)
        if not rate:
            raise HTTPException(status_code=404, detail="Tax rate not found")
        rate.is_active = False
        db.commit()
