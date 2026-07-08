"""Windowed FUP usage reader (#21, A2 + safeguards).

One reader for monthly (QuotaBucket) and daily/weekly (samples/VM). A window
with no throughput points reports ``source="no_data"`` (distinct from a measured
zero) so enforcement won't act on a blind reading.

The reader's sync entry (``get_fup_usage_gb``) bridges async via asyncio.run and
is only ever called from the Celery sweep (no ambient loop). Under pytest's CI
loop we exercise the async variant in a worker thread instead — the same
convention as test_usage_summary.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.bandwidth import BandwidthSample
from app.models.usage import QuotaBucket
from app.services.fup_usage import get_fup_usage_gb_async


def _run(coro):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def _month_bounds(now):
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = (
        datetime(now.year + 1, 1, 1, tzinfo=UTC)
        if now.month == 12
        else datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    )
    return start, end


def test_monthly_reads_quota_bucket(db_session, subscription):
    now = datetime.now(UTC)
    start, end = _month_bounds(now)
    db_session.add(
        QuotaBucket(
            subscription_id=subscription.id,
            period_start=start,
            period_end=end,
            included_gb=Decimal("100.00"),
            used_gb=Decimal("42.50"),
            rollover_gb=Decimal("0.00"),
            overage_gb=Decimal("0.00"),
        )
    )
    db_session.commit()

    usage = _run(get_fup_usage_gb_async(db_session, subscription, "monthly", now=now))
    assert usage.source == "quota_bucket"
    assert usage.is_authoritative is True
    assert abs(usage.used_gb - 42.5) < 0.001
    assert usage.window.period == "monthly"


def test_monthly_missing_bucket_is_zero_nonauthoritative(db_session, subscription):
    usage = _run(get_fup_usage_gb_async(db_session, subscription, "monthly"))
    assert usage.used_gb == 0.0
    assert usage.is_authoritative is False  # no bucket on file


def test_daily_no_samples_is_no_data(db_session, subscription):
    # No samples / metrics store down -> "no_data", distinct from a measured 0,
    # so enforcement won't act on a blind reading (#21 safeguard).
    usage = _run(get_fup_usage_gb_async(db_session, subscription, "daily"))
    assert usage.used_gb == 0.0
    assert usage.source == "no_data"
    assert usage.is_authoritative is False
    assert usage.window.period == "daily"


def test_daily_integrates_recent_samples(db_session, subscription):
    now = datetime.now(UTC)
    if now.hour == 0 and now.minute < 10:
        return  # avoid the rare just-after-UTC-midnight window edge
    for offset in (300, 240, 180):
        db_session.add(
            BandwidthSample(
                subscription_id=subscription.id,
                sample_at=now - timedelta(seconds=offset),
                rx_bps=8_000_000,
                tx_bps=0,
            )
        )
    db_session.commit()

    usage = _run(
        get_fup_usage_gb_async(db_session, subscription, "daily", now=now, tz=UTC)
    )
    assert usage.source == "samples"
    assert usage.is_authoritative is False
    assert usage.used_gb > 0
