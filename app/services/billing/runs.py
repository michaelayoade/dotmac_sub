"""Billing run management service."""

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import BillingRun, BillingRunStatus
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    validate_enum,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


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
        stmt = select(BillingRun)
        if status:
            stmt = stmt.where(
                BillingRun.status == validate_enum(status, BillingRunStatus, "status")
            )
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": BillingRun.created_at, "run_at": BillingRun.run_at},
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())
