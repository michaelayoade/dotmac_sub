"""Service helpers for billing tax-rate web routes."""

from __future__ import annotations

import logging

from app.services import billing as billing_service

logger = logging.getLogger(__name__)

def list_data(db) -> dict[str, object]:
    rates = billing_service.tax_rates.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=200,
        offset=0,
    )
    return {"rates": rates}
