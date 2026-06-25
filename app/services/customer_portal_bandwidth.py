"""Customer portal bandwidth streaming helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.models.bandwidth import BandwidthSample
from app.services.bandwidth import live_event_payload
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
        # True once we have a genuine reading (live traffic from VM, or a recent
        # stored sample — even an idle 0 bps one). Stays False when the poller
        # has no data for this subscription, so the chart shows "Waiting for
        # data" instead of a misleading live 0 bps.
        has_sample = False
        try:
            current = await metrics_store.get_current_bandwidth(str(subscription_id))
            if (current.get("rx_bps", 0) or 0) > 0 or (
                current.get("tx_bps", 0) or 0
            ) > 0:
                has_sample = True
        except Exception:
            logger.debug(
                "Failed to fetch current bandwidth for subscription %s",
                subscription_id,
                exc_info=True,
            )

        try:
            if not has_sample:
                with db_session_adapter.read_session() as sse_db:
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
                        has_sample = True
        except Exception:
            logger.debug(
                "Failed to enrich SSE bandwidth stream for subscription %s",
                subscription_id,
                exc_info=True,
            )

        yield {
            "event": "bandwidth",
            "data": json.dumps(
                live_event_payload(current, datetime.now(UTC), has_sample=has_sample)
            ),
        }
        await asyncio.sleep(1)
