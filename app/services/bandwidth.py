from __future__ import annotations

import builtins
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.bandwidth import BandwidthSample
from app.models.catalog import Subscription
from app.schemas.bandwidth import BandwidthSampleCreate, BandwidthSampleUpdate
from app.services.common import apply_ordering, apply_pagination
from app.services.response import ListResponseMixin, list_response

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
        if agg == "avg":
            agg_fn: Any = func.avg
        elif agg == "max":
            agg_fn = func.max
        elif agg == "min":
            agg_fn = func.min
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid agg. Allowed: avg, max, min",
            )
        bucket = func.date_trunc(interval_map[interval], BandwidthSample.sample_at)
        query = db.query(
            bucket.label("bucket_start"),
            cast(Any, agg_fn)(BandwidthSample.rx_bps).label("rx_bps"),
            cast(Any, agg_fn)(BandwidthSample.tx_bps).label("tx_bps"),
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
            end_at = datetime.now(UTC)
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

        roles = {str(role) for role in (user.get("roles") or [])}
        role_value = user.get("role")
        if isinstance(role_value, str):
            roles.add(role_value)

        # Admins and internal system users can access any subscription.
        if "admin" in roles or user.get("principal_type") == "system_user":
            return subscription

        # Check if subscriber principal owns the subscription (customer portal)
        owner_subscriber_id = (
            user.get("account_id")
            or user.get("subscriber_id")
            or user.get("principal_id")
        )
        if owner_subscriber_id and str(subscription.subscriber_id) == str(owner_subscriber_id):
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
            end_at = datetime.now(UTC)
        if not start_at:
            start_at = end_at - timedelta(hours=24)

        source, step = BandwidthSamples.determine_source_and_step(start_at, end_at)

        if interval != "auto":
            step = interval

        if source == "postgres":
            # Map query step to PostgreSQL bucket size.
            # For near-real-time windows we still want minute buckets; using "hour"
            # for a 1s internal step can collapse fresh data to a single invisible point.
            pg_interval = "hour" if step == "1h" else "minute"
            # Query from PostgreSQL
            rows = BandwidthSamples.series_with_defaults(
                db,
                subscription_id=str(subscription_id),
                device_id=None,
                interface_id=None,
                start_at=start_at,
                end_at=end_at,
                interval=pg_interval,
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
            # If recent raw samples are missing in PostgreSQL, fallback to VictoriaMetrics
            # so charts still render when live/stats are sourced from metrics storage.
            if not data:
                try:
                    from app.services.metrics_store import get_metrics_store

                    metrics_store = get_metrics_store()
                    vm_result = await metrics_store.get_subscription_bandwidth(
                        str(subscription_id), start_at, end_at, step
                    )
                    rx_points = {p.timestamp: p.value for p in vm_result.get("rx", [])}
                    tx_points = {p.timestamp: p.value for p in vm_result.get("tx", [])}
                    all_timestamps = sorted(set(rx_points.keys()) | set(tx_points.keys()))
                    data = [
                        {
                            "timestamp": ts,
                            "rx_bps": rx_points.get(ts, 0),
                            "tx_bps": tx_points.get(ts, 0),
                        }
                        for ts in all_timestamps
                    ]
                    if data:
                        source = "victoriametrics"
                except Exception as e:
                    logger.error(f"VictoriaMetrics fallback query failed: {e}")
        else:
            # Query from VictoriaMetrics
            try:
                from app.services.metrics_store import (
                    get_metrics_store,
                )

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
        end = datetime.now(UTC)
        start = end - duration

        try:
            from app.services.metrics_store import get_metrics_store

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
            sample_count_value = int(sample_count or 0)

            current_rx_bps = float(current.get("rx_bps", 0) or 0)
            current_tx_bps = float(current.get("tx_bps", 0) or 0)
            peak_rx_bps = float(peak.get("rx_peak_bps", 0) or 0)
            peak_tx_bps = float(peak.get("tx_peak_bps", 0) or 0)

            # If metrics store returns zeros but PostgreSQL has samples,
            # fallback to recent PostgreSQL values for current/peak.
            if (
                sample_count_value > 0
                and current_rx_bps <= 0
                and current_tx_bps <= 0
                and peak_rx_bps <= 0
                and peak_tx_bps <= 0
            ):
                latest = (
                    db.query(BandwidthSample)
                    .filter(BandwidthSample.subscription_id == subscription_id)
                    .order_by(BandwidthSample.sample_at.desc())
                    .first()
                )
                pg_stats = (
                    db.query(
                        func.max(BandwidthSample.rx_bps).label("peak_rx"),
                        func.max(BandwidthSample.tx_bps).label("peak_tx"),
                    )
                    .filter(
                        BandwidthSample.subscription_id == subscription_id,
                        BandwidthSample.sample_at >= start,
                    )
                    .first()
                )
                if latest:
                    current_rx_bps = float(latest.rx_bps or 0)
                    current_tx_bps = float(latest.tx_bps or 0)
                if pg_stats:
                    peak_rx_bps = float(pg_stats.peak_rx or 0)
                    peak_tx_bps = float(pg_stats.peak_tx or 0)

            return {
                "current_rx_bps": current_rx_bps,
                "current_tx_bps": current_tx_bps,
                "peak_rx_bps": peak_rx_bps,
                "peak_tx_bps": peak_tx_bps,
                "total_rx_bytes": total["rx_bytes"],
                "total_tx_bytes": total["tx_bytes"],
                "sample_count": sample_count_value,
            }

        except Exception as exc:
            logger.error("Failed to compute bandwidth stats, falling back to PostgreSQL: %s", exc)
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
    ) -> builtins.list[dict[str, object]]:
        """Get top bandwidth consumers with account names.

        Returns:
            List of dicts with subscription_id, total_bps, account_name
        """
        from app.services.metrics_store import MetricsStoreError, get_metrics_store

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
                        subscriber = subscription.subscriber
                        if subscriber.organization:
                            account_name = (
                                subscriber.organization.legal_name
                                or subscriber.organization.name
                            )
                        else:
                            full_name = f"{subscriber.first_name} {subscriber.last_name}".strip()
                            account_name = full_name or subscriber.display_name

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
        subscriber_id = (
            user.get("account_id")
            or user.get("subscriber_id")
            or user.get("principal_id")
        )
        if not subscriber_id:
            raise HTTPException(status_code=403, detail="No account associated with user")

        subscription = (
            db.query(Subscription)
            .filter(
                Subscription.subscriber_id == UUID(str(subscriber_id)),
                Subscription.status.in_(["active", "pending"]),
            )
            .first()
        )

        if not subscription:
            raise HTTPException(status_code=404, detail="No active subscription found")

        return subscription

    @staticmethod
    def get_latest_recent_sample(
        db: Session,
        subscription_id: str | UUID,
        cutoff: datetime,
    ) -> BandwidthSample | None:
        return (
            db.query(BandwidthSample)
            .filter(
                BandwidthSample.subscription_id == subscription_id,
                BandwidthSample.sample_at >= cutoff,
            )
            .order_by(BandwidthSample.sample_at.desc())
            .first()
        )


bandwidth_samples = BandwidthSamples()
