"""Service helpers for billing payment-provider web routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.billing import PaymentProviderType
from app.services import billing as billing_service

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def list_data(db: Session, *, show_inactive: bool) -> dict[str, object]:
    """Build template context for the payment providers list page."""
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=False if show_inactive else None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    return {
        "providers": providers,
        "provider_types": [item.value for item in PaymentProviderType],
        "show_inactive": show_inactive,
    }


def edit_data(db: Session, *, provider_id: str) -> dict[str, object] | None:
    """Build template context for the payment provider edit form."""
    provider = billing_service.payment_providers.get(db, provider_id)
    if not provider:
        return None
    return {
        "provider": provider,
        "provider_types": [item.value for item in PaymentProviderType],
    }
