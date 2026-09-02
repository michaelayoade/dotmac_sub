"""Bandwidth totals must be integrated from samples, not reported as 0.

Regression: get_bandwidth_stats reported total_rx_bytes/total_tx_bytes = 0
whenever the metrics store had no byte totals (the common case), even with
thousands of PostgreSQL samples, so the portal's "total data used" showed 0.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.bandwidth import BandwidthSample
from app.services.bandwidth import bandwidth_samples


def test_estimate_total_bytes_integrates_samples(db_session, subscription):
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    # Two samples 60s apart: avg rx 8000 bps over a 60s span -> 8000/8*60 bytes.
    db_session.add(
        BandwidthSample(
            subscription_id=subscription.id, rx_bps=8000, tx_bps=800, sample_at=base
        )
    )
    db_session.add(
        BandwidthSample(
            subscription_id=subscription.id,
            rx_bps=8000,
            tx_bps=800,
            sample_at=base + timedelta(seconds=60),
        )
    )
    db_session.commit()

    rx, tx = bandwidth_samples._estimate_total_bytes_from_samples(
        db_session, subscription.id, base - timedelta(hours=1)
    )
    assert rx == 60000  # 8000 bps / 8 * 60 s
    assert tx == 6000


def test_estimate_returns_zero_without_samples(db_session, subscription):
    rx, tx = bandwidth_samples._estimate_total_bytes_from_samples(
        db_session, subscription.id, datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert rx == 0
    assert tx == 0


@pytest.mark.asyncio
async def test_stats_releases_read_transaction_before_metrics_store(
    db_session, subscription, monkeypatch
):
    sample_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.add(
        BandwidthSample(
            subscription_id=subscription.id,
            rx_bps=12000,
            tx_bps=3000,
            sample_at=sample_at,
        )
    )
    db_session.commit()

    original_rollback = db_session.rollback
    rollback_count = 0

    def observed_rollback():
        nonlocal rollback_count
        rollback_count += 1
        original_rollback()

    class FakeMetricsStore:
        async def get_current_bandwidth(self, subscription_id: str) -> dict[str, float]:
            assert subscription_id == str(subscription.id)
            assert rollback_count == 1
            return {"rx_bps": 0.0, "tx_bps": 0.0}

        async def get_peak_bandwidth(
            self, subscription_id: str, start: datetime, end: datetime
        ) -> dict[str, float]:
            assert subscription_id == str(subscription.id)
            return {"rx_peak_bps": 0.0, "tx_peak_bps": 0.0}

        async def get_total_bytes(
            self, subscription_id: str, start: datetime, end: datetime
        ) -> dict[str, float]:
            assert subscription_id == str(subscription.id)
            return {"rx_bytes": 0.0, "tx_bytes": 0.0}

    monkeypatch.setattr(db_session, "rollback", observed_rollback)
    monkeypatch.setattr(
        "app.services.metrics_store.get_metrics_store", lambda: FakeMetricsStore()
    )

    stats = await bandwidth_samples.get_bandwidth_stats(
        db_session, subscription.id, "24h"
    )

    assert rollback_count == 1
    assert stats["sample_count"] == 1
    assert stats["current_rx_bps"] == 12000.0
    assert stats["current_tx_bps"] == 3000.0
    assert stats["peak_rx_bps"] == 12000.0
    assert stats["peak_tx_bps"] == 3000.0
