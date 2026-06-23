"""
Bandwidth API Router

Provides endpoints for bandwidth time series data, real-time streaming,
and usage statistics. Supports both admin and customer portal access.
"""

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user, get_db
from app.models.catalog import Subscription
from app.services.bandwidth import (
    add_directions_to_series,
    bandwidth_samples,
    live_event_payload,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.metrics_store import get_metrics_store
from app.services.nas import get_mikrotik_pppoe_live_bandwidth

# The MikroTik poller skips devices with no live viewer when running in
# on_demand mode. Each SSE tick refreshes this subscription's score so the
# poller knows someone is watching.
_ACTIVE_VIEWERS_KEY = os.getenv(
    "BANDWIDTH_ACTIVE_VIEWERS_KEY", "active:bandwidth:viewers"
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bandwidth", tags=["bandwidth"])


# Response schemas
class BandwidthSeriesPoint(BaseModel):
    timestamp: datetime
    rx_bps: float
    tx_bps: float
    # Subscriber-perspective rates (rx/tx above are NAS-perspective). Derived
    # via to_subscriber_directions(); clients must bind to these instead of
    # guessing the rx/tx convention. Without them the chart JS (which reads
    # download_bps/upload_bps exclusively) renders a flat-zero series.
    download_bps: float | None = None
    upload_bps: float | None = None


class BandwidthStats(BaseModel):
    current_rx_bps: float
    current_tx_bps: float
    peak_rx_bps: float
    peak_tx_bps: float
    total_rx_bytes: float
    total_tx_bytes: float
    sample_count: int
    # Subscriber-perspective rates (rx/tx above are NAS-perspective). The
    # service computes these via to_subscriber_directions(); clients should
    # bind to them instead of guessing the rx/tx convention.
    download_bps: float | None = None
    upload_bps: float | None = None
    peak_download_bps: float | None = None
    peak_upload_bps: float | None = None


class TopUserEntry(BaseModel):
    subscription_id: str
    total_bps: float
    account_name: str | None = None


class BandwidthSeriesResponse(BaseModel):
    data: list[BandwidthSeriesPoint]
    total: int
    source: str  # "postgres" or "victoriametrics"


# Admin endpoints
@router.get("/series/{subscription_id}", response_model=BandwidthSeriesResponse)
def get_bandwidth_series(
    subscription_id: UUID,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1s|1m|5m|1h)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth time series for a subscription.

    Automatically selects the appropriate data source based on time range:
    - Last 24 hours: PostgreSQL (raw samples)
    - 1-7 days: VictoriaMetrics (1-minute aggregates)
    - 8-30 days: VictoriaMetrics (5-minute aggregates)
    - 31+ days: VictoriaMetrics (1-hour aggregates)
    """
    bandwidth_samples.check_subscription_access(db, subscription_id, current_user)

    result = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_series,
        db,
        subscription_id,
        start_at,
        end_at,
        interval,
    )

    data = [
        BandwidthSeriesPoint(**point)
        for point in add_directions_to_series(result)["data"]
    ]
    return BandwidthSeriesResponse(
        data=data, total=result["total"], source=result["source"]
    )


@router.get("/stats/{subscription_id}", response_model=BandwidthStats)
def get_bandwidth_stats(
    subscription_id: UUID,
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth statistics for a subscription.

    Returns current, peak, and total bandwidth for the specified period.
    """
    bandwidth_samples.check_subscription_access(db, subscription_id, current_user)

    stats = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_stats,
        db,
        subscription_id,
        period,
    )
    return BandwidthStats(**stats)


@router.get("/mikrotik-live/{subscription_id}")
def get_mikrotik_live_bandwidth(
    subscription_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Read live PPPoE bandwidth directly from the subscription's MikroTik NAS."""
    bandwidth_samples.check_subscription_access(db, subscription_id, current_user)
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if not subscription.provisioning_nas_device:
        raise HTTPException(
            status_code=400, detail="Subscription has no provisioning NAS device"
        )
    return get_mikrotik_pppoe_live_bandwidth(
        subscription.provisioning_nas_device,
        login=subscription.login or "",
    )


@router.get("/live/{subscription_id}")
def get_live_bandwidth(
    subscription_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Server-Sent Events stream for real-time bandwidth updates.

    Sends bandwidth updates approximately every second.
    """
    bandwidth_samples.check_subscription_access(db, subscription_id, current_user)
    # Streaming responses outlive the route function. Release the request-scoped
    # session before the SSE loop starts so a live viewer does not hold a pooled
    # DB connection idle in transaction for the lifetime of the stream.
    db.rollback()
    db.close()

    async def event_generator():
        metrics_store = get_metrics_store()
        redis_client = None
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                import redis.asyncio as aioredis

                redis_client = aioredis.from_url(redis_url)
            except Exception as exc:
                logger.debug("active viewer redis init failed: %s", exc)
                redis_client = None

        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                # Signal the bandwidth poller that this subscription has a live
                # viewer. ZADD overwrites the score, so each tick refreshes the
                # TTL window; the poller treats memberships older than its TTL
                # as gone.
                if redis_client is not None:
                    try:
                        await redis_client.zadd(
                            _ACTIVE_VIEWERS_KEY,
                            {str(subscription_id): time.time()},
                        )
                    except Exception as exc:
                        logger.debug("active viewer heartbeat failed: %s", exc)

                current = {"rx_bps": 0.0, "tx_bps": 0.0}
                try:
                    # Primary source: VictoriaMetrics
                    current = await metrics_store.get_current_bandwidth(
                        str(subscription_id)
                    )
                except Exception as e:
                    logger.warning(
                        "Live bandwidth metrics query failed for %s: %s",
                        subscription_id,
                        e,
                    )

                try:
                    # Fallback source: latest PostgreSQL sample (recent only)
                    if current.get("rx_bps", 0) <= 0 and current.get("tx_bps", 0) <= 0:
                        cutoff = datetime.now(UTC) - timedelta(minutes=2)
                        with db_session_adapter.read_session() as sse_db:
                            latest_sample = bandwidth_samples.get_latest_recent_sample(
                                sse_db, subscription_id, cutoff
                            )
                            if latest_sample:
                                current = {
                                    "rx_bps": float(latest_sample.rx_bps or 0),
                                    "tx_bps": float(latest_sample.tx_bps or 0),
                                }
                except Exception as e:
                    logger.warning(
                        "Live bandwidth DB fallback failed for %s: %s",
                        subscription_id,
                        e,
                    )

                yield {
                    "event": "bandwidth",
                    "data": json.dumps(live_event_payload(current, datetime.now(UTC))),
                }

                await asyncio.sleep(1)
        finally:
            if redis_client is not None:
                try:
                    # Drop our viewer membership immediately on disconnect so
                    # the poller stops within one cycle rather than waiting
                    # for the TTL to expire.
                    await redis_client.zrem(_ACTIVE_VIEWERS_KEY, str(subscription_id))
                    await redis_client.aclose()
                except Exception as exc:
                    logger.debug("active viewer cleanup failed: %s", exc)

    return EventSourceResponse(
        event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/top-users", response_model=list[TopUserEntry])
def get_top_users(
    limit: int = Query(default=10, ge=1, le=100),
    duration: str = Query(default="1h", pattern="^(1h|24h|7d)$"),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[TopUserEntry]:
    """
    Get top bandwidth consumers.

    Returns the top N subscriptions by bandwidth usage.
    Admin only.
    """
    roles = {str(role) for role in (current_user.get("roles") or [])}
    role_value = current_user.get("role")
    if isinstance(role_value, str):
        roles.add(role_value)
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="Admin access required")

    results = cast(
        list[dict[str, object]],
        anyio.from_thread.run(
            bandwidth_samples.get_top_users,
            db,
            limit,
            duration,
        ),
    )
    return [TopUserEntry.model_validate(r) for r in results]


# Customer portal endpoints (own data only)
@router.get("/my/series", response_model=BandwidthSeriesResponse)
def get_my_bandwidth_series(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1m|5m|1h)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth time series for the current user's subscription.

    Customer portal endpoint - returns data for the user's own subscription only.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, current_user)

    result = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_series,
        db,
        subscription.id,
        start_at,
        end_at,
        interval,
    )
    data = [
        BandwidthSeriesPoint(**point)
        for point in add_directions_to_series(result)["data"]
    ]
    return BandwidthSeriesResponse(
        data=data, total=result["total"], source=result["source"]
    )


@router.get("/my/stats", response_model=BandwidthStats)
def get_my_bandwidth_stats(
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth statistics for the current user's subscription.

    Customer portal endpoint.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, current_user)

    stats = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_stats,
        db,
        subscription.id,
        period,
    )
    return BandwidthStats(**stats)


@router.get("/my/live")
def get_my_live_bandwidth(
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Server-Sent Events stream for the current user's bandwidth.

    Customer portal endpoint.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, current_user)

    return get_live_bandwidth(
        subscription_id=subscription.id,
        request=request,
        db=db,
        current_user=current_user,
    )
