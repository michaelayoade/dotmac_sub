"""MRR snapshot service — daily revenue snapshot generation."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.mrr_snapshot import MrrSnapshot
from app.models.subscriber import Subscriber, SubscriberStatus

logger = logging.getLogger(__name__)


class MrrSnapshotManager:
    """Generates and queries MRR snapshots."""

    @staticmethod
    def take_snapshot(db: Session, snapshot_date: date | None = None) -> dict[str, int]:
        """Take an MRR snapshot for all active subscribers.

        Returns:
            Statistics dict with created, updated, skipped counts.
        """
        snapshot_date = snapshot_date or date.today()
        created = 0
        updated = 0
        skipped = 0

        # Get all active subscribers
        stmt = select(Subscriber.id).where(
            Subscriber.status == SubscriberStatus.active,
        )
        subscriber_ids = list(db.scalars(stmt).all())

        for sub_id in subscriber_ids:
            # Sum subscription unit_price for active subscriptions.
            mrr_stmt = select(
                func.coalesce(func.sum(Subscription.unit_price), Decimal("0")),
                func.count(Subscription.id),
            ).where(
                Subscription.subscriber_id == sub_id,
                Subscription.status == SubscriptionStatus.active,
            )
            row = db.execute(mrr_stmt).first()
            mrr_amount = row[0] if row else Decimal("0")
            active_count = row[1] if row else 0

            if mrr_amount == 0 and active_count == 0:
                skipped += 1
                continue

            # Check for existing snapshot
            existing = db.scalars(
                select(MrrSnapshot).where(
                    MrrSnapshot.subscriber_id == sub_id,
                    MrrSnapshot.snapshot_date == snapshot_date,
                )
            ).first()

            # Update cached mrr_total on subscriber
            subscriber = db.get(Subscriber, sub_id)
            if subscriber and subscriber.mrr_total != mrr_amount:
                subscriber.mrr_total = mrr_amount

            if existing:
                existing.mrr_amount = mrr_amount
                existing.active_subscriptions = active_count
                updated += 1
            else:
                snapshot = MrrSnapshot(
                    subscriber_id=sub_id,
                    snapshot_date=snapshot_date,
                    mrr_amount=mrr_amount,
                    active_subscriptions=active_count,
                )
                db.add(snapshot)
                created += 1

        db.flush()
        logger.info(
            "MRR snapshot for %s: created=%d updated=%d skipped=%d",
            snapshot_date,
            created,
            updated,
            skipped,
        )
        return {"created": created, "updated": updated, "skipped": skipped}

    @staticmethod
    def get_subscriber_history(
        db: Session,
        subscriber_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 365,
    ) -> list[MrrSnapshot]:
        """Get MRR history for a subscriber."""
        stmt = select(MrrSnapshot).where(MrrSnapshot.subscriber_id == subscriber_id)
        if start_date:
            stmt = stmt.where(MrrSnapshot.snapshot_date >= start_date)
        if end_date:
            stmt = stmt.where(MrrSnapshot.snapshot_date <= end_date)
        stmt = stmt.order_by(MrrSnapshot.snapshot_date.desc()).limit(limit)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get_total_mrr(db: Session, snapshot_date: date | None = None) -> Decimal:
        """Get total MRR across all subscribers for a given date."""
        snapshot_date = snapshot_date or date.today()
        stmt = select(
            func.coalesce(func.sum(MrrSnapshot.mrr_amount), Decimal("0"))
        ).where(MrrSnapshot.snapshot_date == snapshot_date)
        return db.scalar(stmt) or Decimal("0")

    @staticmethod
    def get_mrr_trend(
        db: Session,
        *,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """Get daily total MRR between dates."""
        stmt = (
            select(
                MrrSnapshot.snapshot_date,
                func.sum(MrrSnapshot.mrr_amount).label("total_mrr"),
                func.count(MrrSnapshot.subscriber_id).label("subscriber_count"),
            )
            .where(
                MrrSnapshot.snapshot_date >= start_date,
                MrrSnapshot.snapshot_date <= end_date,
            )
            .group_by(MrrSnapshot.snapshot_date)
            .order_by(MrrSnapshot.snapshot_date)
        )
        rows = db.execute(stmt).all()
        return [
            {
                "date": row.snapshot_date,
                "total_mrr": row.total_mrr,
                "subscriber_count": row.subscriber_count,
            }
            for row in rows
        ]


mrr_snapshots = MrrSnapshotManager()
