"""Bandwidth totals must be integrated from samples, not reported as 0.

Regression: get_bandwidth_stats reported total_rx_bytes/total_tx_bytes = 0
whenever the metrics store had no byte totals (the common case), even with
thousands of PostgreSQL samples, so the portal's "total data used" showed 0.
"""

from datetime import UTC, datetime, timedelta

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
