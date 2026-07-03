"""Bulk tariff plan change service."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _recurring_price(db: Session, offer_id: str):
    """Active recurring price for an offer, or the newest active price."""
    from app.services import catalog as catalog_service

    prices = catalog_service.offer_prices.list(
        db=db,
        offer_id=offer_id,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    recurring = next(
        (item for item in prices if item.price_type.value == "recurring"), None
    )
    return recurring or (prices[0] if prices else None)


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
    ) -> dict:
        """Preview what will happen if we change all subscribers from source to target plan.

        The change is applied immediately on execute — there is deliberately no
        start-date or balance option (previous form fields collected both and
        ignored them, implying behavior that never existed).

        Returns dict with:
        - source_offer: CatalogOffer
        - target_offer: CatalogOffer
        - affected_subscriptions: list of Subscription objects
        - total_count: int
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

        source_price = _recurring_price(db, source_offer_id)
        target_price = _recurring_price(db, target_offer_id)
        price_delta = None
        if source_price is not None and target_price is not None:
            price_delta = target_price.amount - source_price.amount

        return {
            "source_offer": source,
            "target_offer": target,
            "affected_subscriptions": subscriptions,
            "total_count": len(subscriptions),
            "source_price": source_price,
            "target_price": target_price,
            "price_delta": price_delta,
        }

    @staticmethod
    def execute(
        db: Session,
        *,
        source_offer_id: str,
        target_offer_id: str,
    ) -> dict:
        """Execute the bulk tariff change (immediately).

        Returns dict with: changed, skipped, errors counts plus failed_ids so a
        partial failure is triageable from the UI, not just "check the logs".
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
        changed_ids: list[str] = []
        failed_ids: list[str] = []

        for sub in subscriptions:
            savepoint = db.begin_nested()
            try:
                previous_offer_id = sub.offer_id
                sub.offer_id = target_uuid
                from app.services.catalog.subscriptions import (
                    apply_offer_radius_profile,
                )

                apply_offer_radius_profile(
                    db,
                    sub,
                    previous_offer_id=previous_offer_id,
                )
                savepoint.commit()
                changed += 1
                changed_ids.append(str(sub.id))
            except Exception as e:
                savepoint.rollback()
                logger.error("Error changing subscription %s: %s", sub.id, e)
                errors += 1
                failed_ids.append(str(sub.id))

        if changed > 0:
            db.commit()
            from app.services.enforcement import update_subscription_sessions
            from app.services.radius import reconcile_subscription_connectivity

            for subscription_id in changed_ids:
                try:
                    reconcile_subscription_connectivity(db, subscription_id)
                    update_subscription_sessions(
                        db, subscription_id, reason="profile_change"
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to refresh RADIUS state for subscription %s after bulk tariff change: %s",
                        subscription_id,
                        exc,
                    )

        logger.info(
            "Bulk tariff change: %d changed, %d skipped, %d errors (source=%s, target=%s)",
            changed,
            skipped,
            errors,
            source_offer_id,
            target_offer_id,
        )

        return {
            "changed": changed,
            "skipped": skipped,
            "errors": errors,
            "failed_ids": failed_ids,
        }

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
