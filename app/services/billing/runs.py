"""Billing run management service."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import BillingRun, BillingRunStatus
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    validate_enum,
)
from app.services.response import ListResponseMixin


class BillingRuns(ListResponseMixin):
    @staticmethod
    def get(db: Session, run_id: str):
        run = get_by_id(db, BillingRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Billing run not found")
        return run

    @staticmethod
    def list(
        db: Session,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BillingRun)
        if status:
            query = query.filter(
                BillingRun.status
                == validate_enum(status, BillingRunStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BillingRun.created_at, "run_at": BillingRun.run_at},
        )
        return apply_pagination(query, limit, offset).all()
