"""Backward-compatible subscriber admin route aliases.

Legacy tests and modules still import ``app.web.admin.subscribers``.
The subscriber-facing admin routes now live in ``customers``.
"""

from fastapi import Body, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.web.admin.customers import (
    geocode_address,
    router,
)
from app.web.admin.customers import (
    geocode_primary_address as geocode_primary_address_for_customer,
)


def geocode_primary_address(
    subscriber_id,
    latitude: float = Body(...),
    longitude: float = Body(...),
    db: Session = Depends(get_db),
):
    return geocode_primary_address_for_customer(
        customer_id=str(subscriber_id),
        latitude=latitude,
        longitude=longitude,
        db=db,
    )


__all__ = ["router", "geocode_address", "geocode_primary_address"]
