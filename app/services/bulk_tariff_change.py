"""Bulk tariff plan change service."""
from __future__ import annotations

import logging
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


class BulkTariffChange:
    """Service for bulk tariff plan changes."""

    @staticmethod
    def list_offers(db: Session) -> list[CatalogOffer]:
        """List active catalog offers for selection."""
        stmt = (
            select(CatalogOffer)
            .where(CatalogOffer.is_active.is_(True))
            .order_by(CatalogOffer.name)
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def preview(
        db: Session,
        *,
        source_offer_id: str,
        target_offer_id: str,
        start_date: date,
        ignore_balance: bool = False,
    ) -> dict:
        """Preview what will happen if we change all subscribers from source to target plan.

        Returns dict with:
        - source_offer: CatalogOffer
        - target_offer: CatalogOffer
        - affected_subscriptions: list of Subscription objects
        - total_count: int
        - start_date: date
        """
        source = db.get(CatalogOffer, coerce_uuid(source_offer_id))
        if not source:
            raise HTTPException(status_code=404, detail="Source offer not found")
        target = db.get(CatalogOffer, coerce_uuid(target_offer_id))
        if not target:
            raise HTTPException(status_code=404, detail="Target offer not found")

        stmt = select(Subscription).where(
            Subscription.offer_id == coerce_uuid(source_offer_id),
            Subscription.status == SubscriptionStatus.active,
        )
        subscriptions = list(db.scalars(stmt).all())

        return {
            "source_offer": source,
            "target_offer": target,
            "affected_subscriptions": subscriptions,
            "total_count": len(subscriptions),
            "start_date": start_date,
            "ignore_balance": ignore_balance,
        }

    @staticmethod
    def execute(
        db: Session,
        *,
        source_offer_id: str,
        target_offer_id: str,
        start_date: date,
        ignore_balance: bool = False,
    ) -> dict:
        """Execute the bulk tariff change.

        Returns dict with: changed, skipped, errors counts.
        """
        source_uuid = coerce_uuid(source_offer_id)
        target_uuid = coerce_uuid(target_offer_id)

        target = db.get(CatalogOffer, target_uuid)
        if not target:
            raise HTTPException(status_code=404, detail="Target offer not found")

        stmt = select(Subscription).where(
            Subscription.offer_id == source_uuid,
            Subscription.status == SubscriptionStatus.active,
        )
        subscriptions = list(db.scalars(stmt).all())

        changed = 0
        skipped = 0
        errors = 0

        for sub in subscriptions:
            try:
                sub.offer_id = target_uuid
                changed += 1
            except Exception as e:
                logger.error("Error changing subscription %s: %s", sub.id, e)
                errors += 1

        if changed > 0:
            db.commit()

        logger.info(
            "Bulk tariff change: %d changed, %d skipped, %d errors (source=%s, target=%s)",
            changed,
            skipped,
            errors,
            source_offer_id,
            target_offer_id,
        )

        return {"changed": changed, "skipped": skipped, "errors": errors}

    @staticmethod
    def count_by_offer(db: Session) -> dict[str, int]:
        """Count active subscriptions per offer."""
        stmt = (
            select(
                Subscription.offer_id,
                func.count(Subscription.id),
            )
            .where(Subscription.status == SubscriptionStatus.active)
            .group_by(Subscription.offer_id)
        )
        return {str(row[0]): row[1] for row in db.execute(stmt).all()}


bulk_tariff_change = BulkTariffChange()
