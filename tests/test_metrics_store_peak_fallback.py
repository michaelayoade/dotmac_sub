"""Peak-bandwidth range fallback.

The exact ``max_over_time`` instant query can return nothing on a long
lookbehind window (VictoriaMetrics rejects an over-long ``[duration]`` rollup,
or the data falls outside the instant evaluation's staleness window). When that
happens ``get_peak_bandwidth`` must fall back to the max of the *range* series
— the same query path the chart uses — so the "Peak" tile shows a real figure
instead of a blank when throughput data exists.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.services.metrics_store import MetricsStore, TimeSeriesPoint


def _window(days=24):
    end = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    return end - timedelta(days=days), end


def test_instant_peak_used_when_present():
    store = MetricsStore(base_url="http://vm.test")

    async def fake_instant(query):
        # Both rx and tx return an instant value.
        return [{"value": [0, "5000000" if "rx" in query else "1000000"]}]

    async def fake_range(*a, **k):  # pragma: no cover - must not be needed
        raise AssertionError("range fallback should not run when instant works")

    store.get_instant = fake_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = fake_range  # type: ignore[assignment]

    start, end = _window()
    peak = asyncio.run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 5000000.0, "tx_peak_bps": 1000000.0}


def test_range_fallback_when_instant_empty():
    store = MetricsStore(base_url="http://vm.test")

    async def empty_instant(query):
        return []  # the long-window instant query yields nothing

    async def fake_range(subscription_id, start, end, step="1m"):
        return {
            "rx": [
                TimeSeriesPoint(timestamp=start, value=2_000_000.0),
                TimeSeriesPoint(timestamp=end, value=7_500_000.0),  # peak
            ],
            "tx": [
                TimeSeriesPoint(timestamp=start, value=900_000.0),
                TimeSeriesPoint(timestamp=end, value=1_200_000.0),  # peak
            ],
        }

    store.get_instant = empty_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = fake_range  # type: ignore[assignment]

    start, end = _window()
    peak = asyncio.run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 7_500_000.0, "tx_peak_bps": 1_200_000.0}


def test_range_fallback_when_instant_raises():
    store = MetricsStore(base_url="http://vm.test")

    async def boom_instant(query):
        raise RuntimeError("VM 422: too-long rollup window")

    async def fake_range(subscription_id, start, end, step="1m"):
        return {
            "rx": [TimeSeriesPoint(timestamp=start, value=3_300_000.0)],
            "tx": [TimeSeriesPoint(timestamp=start, value=800_000.0)],
        }

    store.get_instant = boom_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = fake_range  # type: ignore[assignment]

    start, end = _window()
    peak = asyncio.run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 3_300_000.0, "tx_peak_bps": 800_000.0}


def test_no_data_anywhere_returns_zero():
    store = MetricsStore(base_url="http://vm.test")

    async def empty_instant(query):
        return []

    async def empty_range(subscription_id, start, end, step="1m"):
        return {"rx": [], "tx": []}

    store.get_instant = empty_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = empty_range  # type: ignore[assignment]

    start, end = _window()
    peak = asyncio.run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 0.0, "tx_peak_bps": 0.0}
