"""Service helpers for billing tax-rate web routes."""

from __future__ import annotations

import logging

from app.services import billing as billing_service

logger = logging.getLogger(__name__)

def list_data(db) -> dict[str, object]:
    """List all tax rates (active first, then inactive) for the admin UI."""
    active = billing_service.tax_rates.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=200, offset=0,
    )
    inactive = billing_service.tax_rates.list(
        db=db, is_active=False, order_by="name", order_dir="asc", limit=200, offset=0,
    )
    return {"rates": active + inactive}
