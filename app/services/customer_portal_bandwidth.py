"""Customer portal bandwidth streaming helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.models.bandwidth import BandwidthSample
from app.services.db_session_adapter import db_session_adapter
from app.services.metrics_store import get_metrics_store

logger = logging.getLogger(__name__)


async def live_bandwidth_events(
    *,
    subscription_id,
    is_disconnected: Callable[[], Awaitable[bool]],
):
    """Yield SSE bandwidth events for a subscription."""
    metrics_store = get_metrics_store()

    while True:
        if await is_disconnected():
            break

        current = {"rx_bps": 0.0, "tx_bps": 0.0}
        try:
            current = await metrics_store.get_current_bandwidth(str(subscription_id))
        except Exception:
            logger.debug(
                "Failed to fetch current bandwidth for subscription %s",
                subscription_id,
                exc_info=True,
            )

        try:
            if current.get("rx_bps", 0) <= 0 and current.get("tx_bps", 0) <= 0:
                sse_db = db_session_adapter.create_session()
                try:
                    cutoff = datetime.now(UTC) - timedelta(minutes=2)
                    latest_sample = (
                        sse_db.query(BandwidthSample)
                        .filter(
                            BandwidthSample.subscription_id == subscription_id,
                            BandwidthSample.sample_at >= cutoff,
                        )
                        .order_by(BandwidthSample.sample_at.desc())
                        .first()
                    )
                    if latest_sample:
                        current = {
                            "rx_bps": float(latest_sample.rx_bps or 0),
                            "tx_bps": float(latest_sample.tx_bps or 0),
                        }
                finally:
                    sse_db.close()
        except Exception:
            logger.debug(
                "Failed to enrich SSE bandwidth stream for subscription %s",
                subscription_id,
                exc_info=True,
            )

        now = datetime.now(UTC)
        yield {
            "event": "bandwidth",
            "data": json.dumps(
                {
                    "timestamp": now.isoformat(),
                    "rx_bps": float(current.get("rx_bps", 0) or 0),
                    "tx_bps": float(current.get("tx_bps", 0) or 0),
                }
            ),
        }
        await asyncio.sleep(1)
