"""Reviewed bulk tariff scheduling over the subscription lifecycle owner."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import asdict
from typing import cast

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.services.common import coerce_uuid
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
)
from app.services.subscription_lifecycle_batch import (
    MAX_BATCH_SIZE,
    SubscriptionBatchPreviewItem,
    execute_subscription_batch,
    preview_subscription_batch,
)

logger = logging.getLogger(__name__)

_COMMAND_SOURCE = "admin:bulk_tariff_change"
_COMMAND_REASON = "Reviewed bulk tariff change from the admin catalog"


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


def _chunks(values: list[str]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(values[offset : offset + MAX_BATCH_SIZE])
        for offset in range(0, len(values), MAX_BATCH_SIZE)
    )


def _lifecycle_preview_items(
    db: Session,
    subscriptions: list[Subscription],
    *,
    target_offer_id: str,
) -> tuple[SubscriptionBatchPreviewItem, ...]:
    items: list[SubscriptionBatchPreviewItem] = []
    subscription_ids = [str(subscription.id) for subscription in subscriptions]
    for batch in _chunks(subscription_ids):
        preview = preview_subscription_batch(
            db,
            batch,
            kind=SubscriptionCommandKind.change_plan,
            source=_COMMAND_SOURCE,
            target_offer_id=target_offer_id,
            effective_timing=SubscriptionEffectiveTiming.next_cycle,
            reason=_COMMAND_REASON,
        )
        items.extend(preview.items)
    return tuple(items)


def _preview_fingerprint(
    *,
    source_offer_id: str,
    target_offer_id: str,
    include_suspended: bool,
    lifecycle_items: tuple[SubscriptionBatchPreviewItem, ...],
) -> str:
    payload = {
        "source_offer_id": str(coerce_uuid(source_offer_id)),
        "target_offer_id": str(coerce_uuid(target_offer_id)),
        "include_suspended": include_suspended,
        "effective_timing": SubscriptionEffectiveTiming.next_cycle.value,
        "items": [asdict(item) for item in lifecycle_items],
    }
    encoded = json.dumps(
        payload,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    def _eligible_statuses(include_suspended: bool) -> list[SubscriptionStatus]:
        """Statuses a bulk tariff change applies to.

        Default is active-only (byte-identical to prior behavior); opting in adds
        suspended subscriptions to the set.
        """
        statuses = [SubscriptionStatus.active]
        if include_suspended:
            statuses.append(SubscriptionStatus.suspended)
        return statuses

    @staticmethod
    def preview(
        db: Session,
        *,
        source_offer_id: str,
        target_offer_id: str,
        include_suspended: bool = False,
    ) -> dict:
        """Preview a source-offer cohort against one target offer.

        The canonical lifecycle owner previews every subscription. Confirmation
        schedules eligible changes for each subscription's next billing boundary;
        it never performs an unreviewed immediate price or access mutation.

        When ``include_suspended`` is true, suspended subscriptions on the source
        plan are included alongside active ones; otherwise only active ones match.

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

        stmt = (
            select(Subscription)
            .where(
                Subscription.offer_id == coerce_uuid(source_offer_id),
                Subscription.status.in_(
                    BulkTariffChange._eligible_statuses(include_suspended)
                ),
            )
            .order_by(Subscription.id)
        )
        subscriptions = list(db.scalars(stmt).all())
        lifecycle_items = _lifecycle_preview_items(
            db,
            subscriptions,
            target_offer_id=target_offer_id,
        )

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
            "include_suspended": include_suspended,
            "lifecycle_items": lifecycle_items,
            "lifecycle_by_id": {
                item.subscription_id: item for item in lifecycle_items
            },
            "eligible_count": sum(item.eligible for item in lifecycle_items),
            "ineligible_count": sum(not item.eligible for item in lifecycle_items),
            "preview_fingerprint": _preview_fingerprint(
                source_offer_id=source_offer_id,
                target_offer_id=target_offer_id,
                include_suspended=include_suspended,
                lifecycle_items=lifecycle_items,
            ),
        }

    @staticmethod
    def execute(
        db: Session,
        *,
        source_offer_id: str,
        target_offer_id: str,
        include_suspended: bool = False,
        preview_fingerprint: str,
        idempotency_key: str,
        actor_id: str | None,
    ) -> dict:
        """Schedule one preview-bound next-cycle command per subscription.

        Every item passes through the subscription lifecycle validation, billing
        treatment, scheduling, idempotency, audit, and eventual access projection
        boundary. A changed cohort or lifecycle head invalidates confirmation.
        """
        source_uuid = coerce_uuid(source_offer_id)
        target_uuid = coerce_uuid(target_offer_id)

        source = db.get(CatalogOffer, source_uuid)
        if not source:
            raise HTTPException(status_code=404, detail="Source offer not found")
        target = db.get(CatalogOffer, target_uuid)
        if not target:
            raise HTTPException(status_code=404, detail="Target offer not found")
        if source_uuid == target_uuid:
            raise HTTPException(
                status_code=400,
                detail="Source and target offers must be different",
            )
        operation_key = idempotency_key.strip()
        if not operation_key:
            raise HTTPException(
                status_code=400,
                detail="A preview operation key is required",
            )
        execution_actor = str(actor_id or "").strip()
        if not execution_actor:
            raise HTTPException(
                status_code=401,
                detail="An authenticated actor is required for bulk tariff changes",
            )

        current = BulkTariffChange.preview(
            db,
            source_offer_id=str(source_uuid),
            target_offer_id=str(target_uuid),
            include_suspended=include_suspended,
        )
        expected_fingerprint = str(preview_fingerprint or "").strip()
        current_fingerprint = str(current["preview_fingerprint"])
        if not expected_fingerprint or not hmac.compare_digest(
            current_fingerprint,
            expected_fingerprint,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "The subscription cohort or lifecycle facts changed after "
                    "preview. Preview the bulk tariff change again."
                ),
            )

        lifecycle_items = cast(
            tuple[SubscriptionBatchPreviewItem, ...],
            current["lifecycle_items"],
        )
        reviewed_heads = {
            item.subscription_id: item.expected_head
            for item in lifecycle_items
            if item.expected_head is not None
        }
        outcome_items = []
        subscription_ids = [str(item.subscription_id) for item in lifecycle_items]
        for batch_number, batch in enumerate(_chunks(subscription_ids), start=1):
            outcome = execute_subscription_batch(
                db,
                batch,
                kind=SubscriptionCommandKind.change_plan,
                source=_COMMAND_SOURCE,
                actor_id=execution_actor,
                target_offer_id=str(target_uuid),
                effective_timing=SubscriptionEffectiveTiming.next_cycle,
                reason=_COMMAND_REASON,
                reviewed_heads=reviewed_heads,
                idempotency_key=f"{operation_key}:batch:{batch_number}",
            )
            outcome_items.extend(outcome.items)

        changed_statuses = {
            SubscriptionCommandOutcomeStatus.applied,
            SubscriptionCommandOutcomeStatus.scheduled,
        }
        failed_ids = [
            item.subscription_id
            for item in outcome_items
            if item.status == SubscriptionCommandOutcomeStatus.failed
        ]
        changed_ids = [
            item.subscription_id
            for item in outcome_items
            if item.status in changed_statuses
        ]
        skipped_ids = [
            item.subscription_id
            for item in outcome_items
            if item.status not in changed_statuses
            and item.status != SubscriptionCommandOutcomeStatus.failed
        ]

        logger.info(
            "Bulk tariff change: %d scheduled, %d skipped, %d errors "
            "(source=%s, target=%s)",
            len(changed_ids),
            len(skipped_ids),
            len(failed_ids),
            source_offer_id,
            target_offer_id,
        )

        return {
            "changed": len(changed_ids),
            "skipped": len(skipped_ids),
            "errors": len(failed_ids),
            "changed_ids": changed_ids,
            "skipped_ids": skipped_ids,
            "failed_ids": failed_ids,
            "outcomes": outcome_items,
            "effective_timing": SubscriptionEffectiveTiming.next_cycle.value,
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
