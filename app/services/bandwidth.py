from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.bandwidth import BandwidthSample
from app.models.catalog import Subscription
from app.services.common import apply_ordering, apply_pagination
from app.schemas.bandwidth import BandwidthSampleCreate, BandwidthSampleUpdate
from app.services.response import list_response
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class BandwidthSamples(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: BandwidthSampleCreate):
        sample = BandwidthSample(**payload.model_dump())
        db.add(sample)
        db.commit()
        db.refresh(sample)
        return sample

    @staticmethod
    def get(db: Session, sample_id: str):
        sample = db.get(BandwidthSample, sample_id)
        if not sample:
            raise HTTPException(status_code=404, detail="Bandwidth sample not found")
        return sample

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        device_id: str | None,
        interface_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BandwidthSample)
        if subscription_id:
            query = query.filter(BandwidthSample.subscription_id == subscription_id)
        if device_id:
            query = query.filter(BandwidthSample.device_id == device_id)
        if interface_id:
            query = query.filter(BandwidthSample.interface_id == interface_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BandwidthSample.created_at, "sample_at": BandwidthSample.sample_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, sample_id: str, payload: BandwidthSampleUpdate):
        sample = db.get(BandwidthSample, sample_id)
        if not sample:
            raise HTTPException(status_code=404, detail="Bandwidth sample not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(sample, key, value)
        db.commit()
        db.refresh(sample)
        return sample

    @staticmethod
    def delete(db: Session, sample_id: str):
        sample = db.get(BandwidthSample, sample_id)
        if not sample:
            raise HTTPException(status_code=404, detail="Bandwidth sample not found")
        db.delete(sample)
        db.commit()

    @staticmethod
    def series(
        db: Session,
        subscription_id: str | None,
        device_id: str | None,
        interface_id: str | None,
        start_at,
        end_at,
        interval: str,
        agg: str,
    ):
        interval_map = {"minute": "minute", "hour": "hour", "day": "day"}
        if interval not in interval_map:
            raise HTTPException(
                status_code=400,
                detail="Invalid interval. Allowed: minute, hour, day",
            )
        agg_map = {"avg": func.avg, "max": func.max, "min": func.min}
        if agg not in agg_map:
            raise HTTPException(
                status_code=400,
                detail="Invalid agg. Allowed: avg, max, min",
            )
        bucket = func.date_trunc(interval_map[interval], BandwidthSample.sample_at)
        query = db.query(
            bucket.label("bucket_start"),
            agg_map[agg](BandwidthSample.rx_bps).label("rx_bps"),
            agg_map[agg](BandwidthSample.tx_bps).label("tx_bps"),
        )
        if subscription_id:
            query = query.filter(BandwidthSample.subscription_id == subscription_id)
        if device_id:
            query = query.filter(BandwidthSample.device_id == device_id)
        if interface_id:
            query = query.filter(BandwidthSample.interface_id == interface_id)
        if start_at:
            query = query.filter(BandwidthSample.sample_at >= start_at)
        if end_at:
            query = query.filter(BandwidthSample.sample_at <= end_at)
        query = query.group_by(bucket).order_by(bucket.asc())
        return query.all()

    @staticmethod
    def series_with_defaults(
        db: Session,
        subscription_id: str | None,
        device_id: str | None,
        interface_id: str | None,
        start_at,
        end_at,
        interval: str,
        agg: str,
    ):
        if start_at is None and end_at is None:
            end_at = datetime.now(timezone.utc)
            start_at = end_at - timedelta(hours=24)
        rows = BandwidthSamples.series(
            db,
            subscription_id,
            device_id,
            interface_id,
            start_at,
            end_at,
            interval,
            agg,
        )
        return [
            {"bucket_start": row.bucket_start, "rx_bps": row.rx_bps, "tx_bps": row.tx_bps}
            for row in rows
        ]

    @staticmethod
    def series_response(
        db: Session,
        subscription_id: str | None,
        device_id: str | None,
        interface_id: str | None,
        start_at,
        end_at,
        interval: str,
        agg: str,
    ):
        rows = BandwidthSamples.series_with_defaults(
            db,
            subscription_id,
            device_id,
            interface_id,
            start_at,
            end_at,
            interval,
            agg,
        )
        return list_response(rows, len(rows), 0)


    @staticmethod
    def check_subscription_access(
        db: Session,
        subscription_id: str | UUID,
        user: dict,
    ) -> Subscription:
        """Check if the user has access to the subscription's bandwidth data.

        Args:
            db: Database session
            subscription_id: The subscription to check access for
            user: The current user dictionary

        Returns:
            The subscription if access is allowed

        Raises:
            HTTPException: If subscription not found or access denied
        """
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")

        # Admin can access any subscription
        if user.get("role") == "admin":
            return subscription

        # Check if user owns the subscription (customer portal)
        user_account_id = user.get("account_id")
        if user_account_id and str(subscription.subscriber_id) == str(user_account_id):
            return subscription

        raise HTTPException(status_code=403, detail="Access denied to this subscription")

    @staticmethod
    def determine_source_and_step(
        start: datetime,
        end: datetime,
    ) -> tuple[str, str]:
        """Determine whether to use PostgreSQL or VictoriaMetrics based on time range.

        Returns:
            Tuple of (source, step) where source is "postgres" or "victoriametrics"
        """
        duration = end - start

        # Use PostgreSQL for last 24 hours (raw data)
        if duration <= timedelta(hours=24):
            return "postgres", "1s"

        # Use VictoriaMetrics for longer ranges
        if duration <= timedelta(days=7):
            return "victoriametrics", "1m"
        elif duration <= timedelta(days=30):
            return "victoriametrics", "5m"
        else:
            return "victoriametrics", "1h"

    @staticmethod
    async def get_bandwidth_series(
        db: Session,
        subscription_id: str | UUID,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        interval: str = "auto",
    ) -> dict:
        """Get bandwidth time series for a subscription.

        Automatically selects the appropriate data source based on time range.

        Returns:
            Dict with keys: data (list of points), total (count), source
        """
        # Default time range
        if not end_at:
            end_at = datetime.now(timezone.utc)
        if not start_at:
            start_at = end_at - timedelta(hours=24)

        source, step = BandwidthSamples.determine_source_and_step(start_at, end_at)

        if interval != "auto":
            step = interval

        if source == "postgres":
            # Query from PostgreSQL
            rows = BandwidthSamples.series_with_defaults(
                db,
                subscription_id=str(subscription_id),
                device_id=None,
                interface_id=None,
                start_at=start_at,
                end_at=end_at,
                interval="minute" if step in ("1m", "auto") else "hour",
                agg="avg",
            )
            data = [
                {
                    "timestamp": row["bucket_start"],
                    "rx_bps": float(row["rx_bps"] or 0),
                    "tx_bps": float(row["tx_bps"] or 0),
                }
                for row in rows
            ]
        else:
            # Query from VictoriaMetrics
            try:
                from app.services.metrics_store import get_metrics_store, MetricsStoreError

                metrics_store = get_metrics_store()
                result = await metrics_store.get_subscription_bandwidth(
                    str(subscription_id), start_at, end_at, step
                )

                # Combine rx and tx series
                rx_points = {p.timestamp: p.value for p in result.get("rx", [])}
                tx_points = {p.timestamp: p.value for p in result.get("tx", [])}

                all_timestamps = sorted(set(rx_points.keys()) | set(tx_points.keys()))
                data = [
                    {
                        "timestamp": ts,
                        "rx_bps": rx_points.get(ts, 0),
                        "tx_bps": tx_points.get(ts, 0),
                    }
                    for ts in all_timestamps
                ]
            except Exception as e:
                logger.error(f"VictoriaMetrics query failed: {e}")
                # Fallback to PostgreSQL
                rows = BandwidthSamples.series_with_defaults(
                    db,
                    subscription_id=str(subscription_id),
                    device_id=None,
                    interface_id=None,
                    start_at=start_at,
                    end_at=end_at,
                    interval="hour",
                    agg="avg",
                )
                data = [
                    {
                        "timestamp": row["bucket_start"],
                        "rx_bps": float(row["rx_bps"] or 0),
                        "tx_bps": float(row["tx_bps"] or 0),
                    }
                    for row in rows
                ]
                source = "postgres"

        return {"data": data, "total": len(data), "source": source}

    @staticmethod
    async def get_bandwidth_stats(
        db: Session,
        subscription_id: str | UUID,
        period: str = "24h",
    ) -> dict:
        """Get bandwidth statistics for a subscription.

        Returns:
            Dict with current, peak, and total bandwidth for the specified period.
        """
        # Parse period
        period_map = {
            "1h": timedelta(hours=1),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }
        duration = period_map.get(period, timedelta(hours=24))
        end = datetime.now(timezone.utc)
        start = end - duration

        try:
            from app.services.metrics_store import get_metrics_store, MetricsStoreError

            metrics_store = get_metrics_store()

            # Get current bandwidth
            current = await metrics_store.get_current_bandwidth(str(subscription_id))

            # Get peak bandwidth
            peak = await metrics_store.get_peak_bandwidth(str(subscription_id), start, end)

            # Get total bytes
            total = await metrics_store.get_total_bytes(str(subscription_id), start, end)

            # Get sample count from PostgreSQL for recent period
            sample_count = (
                db.query(func.count(BandwidthSample.id))
                .filter(
                    BandwidthSample.subscription_id == subscription_id,
                    BandwidthSample.sample_at >= start,
                )
                .scalar()
            )

            return {
                "current_rx_bps": current["rx_bps"],
                "current_tx_bps": current["tx_bps"],
                "peak_rx_bps": peak["rx_peak_bps"],
                "peak_tx_bps": peak["tx_peak_bps"],
                "total_rx_bytes": total["rx_bytes"],
                "total_tx_bytes": total["tx_bytes"],
                "sample_count": sample_count or 0,
            }

        except Exception:
            # Fallback to PostgreSQL-only stats
            stats = (
                db.query(
                    func.max(BandwidthSample.rx_bps).label("peak_rx"),
                    func.max(BandwidthSample.tx_bps).label("peak_tx"),
                    func.avg(BandwidthSample.rx_bps).label("avg_rx"),
                    func.avg(BandwidthSample.tx_bps).label("avg_tx"),
                    func.count().label("count"),
                )
                .filter(
                    BandwidthSample.subscription_id == subscription_id,
                    BandwidthSample.sample_at >= start,
                )
                .first()
            )

            # Get latest sample for current
            latest = (
                db.query(BandwidthSample)
                .filter(BandwidthSample.subscription_id == subscription_id)
                .order_by(BandwidthSample.sample_at.desc())
                .first()
            )

            return {
                "current_rx_bps": float(latest.rx_bps if latest else 0),
                "current_tx_bps": float(latest.tx_bps if latest else 0),
                "peak_rx_bps": float(stats.peak_rx or 0),
                "peak_tx_bps": float(stats.peak_tx or 0),
                "total_rx_bytes": 0,  # Can't easily calculate without VM
                "total_tx_bytes": 0,
                "sample_count": stats.count or 0,
            }

    @staticmethod
    async def get_top_users(
        db: Session,
        limit: int = 10,
        duration: str = "1h",
    ) -> list[dict]:
        """Get top bandwidth consumers with account names.

        Returns:
            List of dicts with subscription_id, total_bps, account_name
        """
        from app.services.metrics_store import get_metrics_store, MetricsStoreError

        try:
            metrics_store = get_metrics_store()
            results = await metrics_store.get_top_users(limit, duration)

            # Enrich with account names
            enriched = []
            for r in results:
                sub_id = r["subscription_id"]
                account_name = None

                if sub_id:
                    subscription = db.get(Subscription, UUID(sub_id))
                    if subscription and subscription.subscriber:
                        account = subscription.subscriber
                        if account.subscriber and account.subscriber.person:
                            account_name = f"{account.subscriber.person.first_name} {account.subscriber.person.last_name}"
                        elif account.subscriber and account.subscriber.organization:
                            account_name = account.subscriber.organization.name

                enriched.append({
                    "subscription_id": sub_id or "unknown",
                    "total_bps": r["total_bps"],
                    "account_name": account_name,
                })

            return enriched

        except MetricsStoreError as e:
            logger.error(f"Failed to get top users: {e}")
            raise HTTPException(status_code=503, detail="Metrics service unavailable")

    @staticmethod
    def get_user_active_subscription(db: Session, user: dict) -> Subscription:
        """Get the active subscription for a user.

        Args:
            db: Database session
            user: Current user dict with account_id

        Returns:
            The user's active subscription

        Raises:
            HTTPException: If no account or active subscription found
        """
        account_id = user.get("account_id")
        if not account_id:
            raise HTTPException(status_code=403, detail="No account associated with user")

        subscription = (
            db.query(Subscription)
            .filter(
                Subscription.subscriber_id == UUID(account_id),
                Subscription.status.in_(["active", "pending"]),
            )
            .first()
        )

        if not subscription:
            raise HTTPException(status_code=404, detail="No active subscription found")

        return subscription


bandwidth_samples = BandwidthSamples()
