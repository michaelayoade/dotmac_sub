"""
Bandwidth API Router

Provides endpoints for bandwidth time series data, real-time streaming,
and usage statistics. Supports both admin and customer portal access.
"""
import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user, get_db
from app.services.bandwidth import bandwidth_samples
from app.services.metrics_store import get_metrics_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bandwidth", tags=["bandwidth"])


# Response schemas
class BandwidthSeriesPoint(BaseModel):
    timestamp: datetime
    rx_bps: float
    tx_bps: float


class BandwidthStats(BaseModel):
    current_rx_bps: float
    current_tx_bps: float
    peak_rx_bps: float
    peak_tx_bps: float
    total_rx_bytes: float
    total_tx_bytes: float
    sample_count: int


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

    data = [BandwidthSeriesPoint(**point) for point in result["data"]]
    return BandwidthSeriesResponse(data=data, total=result["total"], source=result["source"])


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

    async def event_generator():
        metrics_store = get_metrics_store()

        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            try:
                # Get current bandwidth from VictoriaMetrics
                current = await metrics_store.get_current_bandwidth(str(subscription_id))
                now = datetime.now(UTC)

                yield {
                    "event": "bandwidth",
                    "data": {
                        "timestamp": now.isoformat(),
                        "rx_bps": current["rx_bps"],
                        "tx_bps": current["tx_bps"],
                    },
                }

            except Exception as e:
                logger.error(f"Error in live bandwidth stream: {e}")
                yield {
                    "event": "error",
                    "data": {"message": "Error fetching bandwidth data"},
                }

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


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
    if current_user.get("role") != "admin":
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
    data = [BandwidthSeriesPoint(**point) for point in result["data"]]
    return BandwidthSeriesResponse(data=data, total=result["total"], source=result["source"])


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
