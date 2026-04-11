"""Service helpers for billing tax-rate web routes."""

from __future__ import annotations

import logging

from app.schemas.billing import TaxRateCreate
from app.services import billing as billing_service
from app.validators.forms import parse_decimal

logger = logging.getLogger(__name__)


def list_data(db) -> dict[str, object]:
    """List all tax rates (active first, then inactive) for the admin UI."""
    active = billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    inactive = billing_service.tax_rates.list(
        db=db,
        is_active=False,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {"rates": active + inactive}


def create_tax_rate_from_form(
    db,
    *,
    name: str,
    rate: str,
    code: str | None,
    description: str | None,
):
    payload = TaxRateCreate(
        name=name.strip(),
        rate=parse_decimal(rate, "rate"),
        code=code.strip() if code else None,
        description=description.strip() if description else None,
    )
    return billing_service.tax_rates.create(db, payload)


def toggle_tax_rate(db, *, rate_id: str):
    return billing_service.tax_rates.toggle_active(db, rate_id)
