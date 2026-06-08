"""Tests for the time-windowed /me/usage-summary endpoint and service.

Covers the correctness the legacy "sum the last 50 sessions" path lacked: a
defined window, byte integration from throughput samples, and authoritative
totals for cycle (quota) and all (session octets).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api import me as me_api
from app.models.bandwidth import BandwidthSample
from app.models.usage import AccountingStatus, QuotaBucket, RadiusAccountingSession
from app.services import usage_summary as svc


def _run(coro):
    return asyncio.run(coro)


# --- pure helpers ----------------------------------------------------------


def test_integrate_sums_volume_and_buckets_by_minute():
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    points = [
        (base, 8000.0, 800.0),
        (base + timedelta(seconds=60), 8000.0, 800.0),
    ]
    buckets = svc._integrate(points, "minute")
    # (8000+800)/8 * 60s = 66000 bytes, attributed to the 12:00 minute bucket.
    assert buckets == {base: 66000.0}


def test_integrate_skips_gaps_far_above_typical_spacing():
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    # Three 60s-spaced points then a 5000s idle gap. The 60s spacing is typical,
    # so only the anomalous gap is dropped (its 5,000,000-byte segment excluded).
    points = [
        (base, 8000.0, 0.0),
        (base + timedelta(seconds=60), 8000.0, 0.0),
        (base + timedelta(seconds=120), 8000.0, 0.0),
        (base + timedelta(seconds=5120), 8000.0, 0.0),
    ]
    buckets = svc._integrate(points, "minute")
    # Two kept 60s segments (60000 bytes each); the idle gap is not filled.
    assert sum(buckets.values()) == 120000.0


def test_integrate_keeps_regularly_spaced_coarse_series():
    # Hourly VM buckets (>30-day cycle): a fixed 15-min cap would drop every
    # segment. Spacing-relative keeps them.
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    points = [
        (base + timedelta(hours=h), 8000.0, 0.0) for h in range(4)
    ]
    buckets = svc._integrate(points, "day")
    # 3 one-hour segments at 8000 bps -> 8000/8*3600 = 3.6e6 bytes each.
    assert sum(buckets.values()) == 3 * (8000 / 8 * 3600)


def test_truncate_day_and_hour():
    ts = datetime(2026, 6, 1, 13, 37, 5, tzinfo=UTC)
    assert svc._truncate(ts, "hour") == datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    assert svc._truncate(ts, "day") == datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


# --- service against the DB -----------------------------------------------


def test_hour_integrates_bandwidth_samples(db_session, subscriber, subscription):
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    for offset in (0, 60):
        db_session.add(
            BandwidthSample(
                subscription_id=subscription.id,
                rx_bps=8000,
                tx_bps=800,
                sample_at=base + timedelta(seconds=offset),
            )
        )
    db_session.commit()

    now = base + timedelta(seconds=120)
    out = _run(svc.get_usage_summary(db_session, str(subscriber.id), "hour", now=now))

    assert out["period"] == "hour"
    assert out["bucket"] == "minute"
    assert out["total_source"] == "samples"
    assert out["is_authoritative"] is False
    assert out["total_bytes"] == 66000  # (8000+800)/8 * 60
    assert len(out["series"]) == 1
    assert out["series"][0]["bytes"] == 66000


def test_all_sums_session_octets_including_active(
    db_session, subscriber, subscription
):
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="closed-1",
            status_type=AccountingStatus.stop,
            session_start=now - timedelta(days=2),
            session_end=now - timedelta(days=2, hours=-1),
            input_octets=1000,
            output_octets=500,
        )
    )
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="active-1",
            status_type=AccountingStatus.interim,
            session_start=now - timedelta(hours=3),
            session_end=None,  # live session, octets are current
            input_octets=2000,
            output_octets=300,
        )
    )
    db_session.commit()

    out = _run(svc.get_usage_summary(db_session, str(subscriber.id), "all", now=now))

    assert out["total_source"] == "sessions"
    assert out["is_authoritative"] is True
    assert out["bucket"] is None
    assert out["series"] == []
    assert out["total_bytes"] == 1000 + 500 + 2000 + 300  # includes active


def test_cycle_uses_rated_quota_bucket(db_session, subscriber, subscription):
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    db_session.add(
        QuotaBucket(
            subscription_id=subscription.id,
            period_start=now - timedelta(days=5),
            period_end=now + timedelta(days=25),
            used_gb=2,
        )
    )
    db_session.commit()

    out = _run(svc.get_usage_summary(db_session, str(subscriber.id), "cycle", now=now))

    assert out["total_source"] == "quota"
    assert out["is_authoritative"] is True
    assert out["total_bytes"] == 2 * (1024**3)
    assert out["bucket"] == "day"


def test_window_with_no_data_falls_back_without_false_zero(
    db_session, subscriber, subscription
):
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    # A session today but no bandwidth samples: total must not be a false 0.
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="today-1",
            status_type=AccountingStatus.stop,
            session_start=now - timedelta(hours=1),
            session_end=now,
            input_octets=4000,
            output_octets=1000,
        )
    )
    db_session.commit()

    out = _run(svc.get_usage_summary(db_session, str(subscriber.id), "today", now=now))
    assert out["series"] == []
    assert out["total_source"] == "sessions"
    assert out["total_bytes"] == 5000


# --- endpoint scoping ------------------------------------------------------


def test_usage_summary_403_for_non_subscriber():
    principal = {"principal_type": "system_user", "subscriber_id": str(uuid.uuid4())}
    with pytest.raises(HTTPException) as exc:
        _run(me_api.my_usage_summary(period="today", db=None, principal=principal))
    assert exc.value.status_code == 403


def test_usage_summary_scopes_to_caller(monkeypatch):
    principal = {"principal_type": "subscriber", "subscriber_id": str(uuid.uuid4())}
    captured = {}

    async def fake(db, subscriber_id, period, now=None):
        captured["subscriber_id"] = subscriber_id
        captured["period"] = period
        return {
            "period": period,
            "start": datetime.now(UTC),
            "end": datetime.now(UTC),
            "total_bytes": 0,
            "total_source": "samples",
            "is_authoritative": False,
            "bucket": None,
            "series": [],
        }

    monkeypatch.setattr(svc, "get_usage_summary", fake)
    _run(me_api.my_usage_summary(period="week", db=None, principal=principal))
    assert captured["subscriber_id"] == principal["subscriber_id"]
    assert captured["period"] == "week"
