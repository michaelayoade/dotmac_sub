"""Payment-provider configuration service.

Provider-event observation and consequence ownership lives in
``app.services.payment_provider_events``. Keeping configuration separate makes
the trust and transaction boundary explicit.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import PaymentProvider, PaymentProviderType
from app.schemas.billing import PaymentProviderCreate, PaymentProviderUpdate
from app.services.common import apply_ordering, apply_pagination, get_by_id
from app.services.payment_routing import (
    create_configured_provider,
    deactivate_configured_provider,
    update_configured_provider,
)
from app.services.response import ListResponseMixin


class PaymentProviders(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentProviderCreate):
        return create_configured_provider(db, payload)

    @staticmethod
    def get(db: Session, provider_id: str):
        provider = get_by_id(db, PaymentProvider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Payment provider not found")
        return provider

    @staticmethod
    def get_by_type(
        db: Session, provider_type: PaymentProviderType
    ) -> PaymentProvider | None:
        return (
            db.query(PaymentProvider)
            .filter(PaymentProvider.provider_type == provider_type)
            .order_by(PaymentProvider.created_at.asc())
            .first()
        )

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentProvider)
        if is_active is None:
            query = query.filter(PaymentProvider.is_active.is_(True))
        else:
            query = query.filter(PaymentProvider.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PaymentProvider.created_at, "name": PaymentProvider.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = apply_ordering(
            db.query(PaymentProvider),
            order_by,
            order_dir,
            {"created_at": PaymentProvider.created_at, "name": PaymentProvider.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, provider_id: str, payload: PaymentProviderUpdate):
        provider = get_by_id(db, PaymentProvider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Payment provider not found")
        return update_configured_provider(db, provider, payload)

    @staticmethod
    def delete(db: Session, provider_id: str):
        provider = get_by_id(db, PaymentProvider, provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Payment provider not found")
        deactivate_configured_provider(db, provider)


__all__ = ["PaymentProviders"]
