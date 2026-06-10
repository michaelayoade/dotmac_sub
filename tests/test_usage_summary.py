"""Tests for the time-windowed /me/usage-summary endpoint and service.

Covers the correctness the legacy "sum the last 50 sessions" path lacked: a
defined window, byte integration from throughput samples, and authoritative
totals for cycle (quota) and all (session octets).
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.api import me as me_api
from app.models.bandwidth import BandwidthSample
from app.models.usage import AccountingStatus, QuotaBucket, RadiusAccountingSession
from app.services import usage_summary as svc


def _run_async(coro):
    # Run in a dedicated thread to avoid nested event loops. Project convention
    # (see tests/test_main_domain_routing.py): async-def tests marked
    # @pytest.mark.asyncio conflict with the suite's already-running loop in CI.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


# --- pure helpers ----------------------------------------------------------


def test_integrate_sums_volume_and_buckets_by_minute():
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    points = [
        (base, 8000.0, 800.0),
        (base + timedelta(seconds=60), 8000.0, 800.0),
    ]
    buckets = svc._integrate(points, "minute", UTC)
    # (8000+800)/8 * 60s = 66000 bytes, attributed to the 12:00 minute bucket.
    assert buckets == {base: 66000.0}


def test_integrate_carries_last_rate_to_end_but_caps_it():
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    points = [
        (base, 8000.0, 0.0),
        (base + timedelta(seconds=60), 8000.0, 0.0),
    ]
    # 60s segment + 60s tail carried to end, both at 8000 bps.
    near = svc._integrate(points, "minute", UTC, end=base + timedelta(seconds=120))
    assert sum(near.values()) == 120000.0
    # A far end exceeds the gap cap, so no phantom tail is fabricated.
    far = svc._integrate(points, "minute", UTC, end=base + timedelta(seconds=100000))
    assert sum(far.values()) == 60000.0


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
    buckets = svc._integrate(points, "minute", UTC)
    # Two kept 60s segments (60000 bytes each); the idle gap is not filled.
    assert sum(buckets.values()) == 120000.0


def test_integrate_keeps_regularly_spaced_coarse_series():
    # Hourly VM buckets (>30-day cycle): a fixed 15-min cap would drop every
    # segment. Spacing-relative keeps them.
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    points = [(base + timedelta(hours=h), 8000.0, 0.0) for h in range(4)]
    buckets = svc._integrate(points, "day", UTC)
    # 3 one-hour segments at 8000 bps -> 8000/8*3600 = 3.6e6 bytes each.
    assert sum(buckets.values()) == 3 * (8000 / 8 * 3600)


def test_truncate_day_and_hour():
    ts = datetime(2026, 6, 1, 13, 37, 5, tzinfo=UTC)
    assert svc._truncate(ts, "hour", UTC) == datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    assert svc._truncate(ts, "day", UTC) == datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def test_truncate_is_dst_safe():
    ny = ZoneInfo("America/New_York")
    # Spring-forward day: the afternoon is EDT (-4) but that day's midnight is
    # still EST (-5), so local midnight is 05:00 UTC, not 04:00.
    afternoon = datetime(2025, 3, 9, 15, 0, tzinfo=ny)
    assert svc._truncate(afternoon, "day", ny) == datetime(2025, 3, 9, 5, 0, tzinfo=UTC)
    # Fall-back day: 01:30 occurs twice (EDT then EST). Both must collapse to a
    # single canonical hour bucket rather than two same-labelled bars.
    first = datetime(2025, 11, 2, 1, 30, fold=0, tzinfo=ny)
    second = datetime(2025, 11, 2, 1, 30, fold=1, tzinfo=ny)
    assert svc._truncate(first, "hour", ny) == svc._truncate(second, "hour", ny)
    assert svc._truncate(second, "hour", ny) == datetime(2025, 11, 2, 5, 0, tzinfo=UTC)


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
    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "hour", now=now)
    )

    assert out["period"] == "hour"
    assert out["bucket"] == "minute"
    assert out["total_source"] == "samples"
    assert out["is_authoritative"] is False
    # 12:00→12:01 segment plus the tail carried to now (12:02): 2 × 66000.
    assert out["total_bytes"] == 132000
    assert len(out["series"]) == 2
    assert out["series"][0]["bytes"] == 66000


def test_all_sums_session_octets_including_active(db_session, subscriber, subscription):
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

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "all", now=now)
    )

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

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "cycle", now=now)
    )

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

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "today", now=now)
    )
    assert out["series"] == []
    assert out["total_source"] == "sessions"
    assert out["total_bytes"] == 5000


# --- endpoint scoping ------------------------------------------------------


# --- customer-facing FUP summary -------------------------------------------


def test_fup_summary_none_without_subscriptions(db_session, subscriber):
    # A subscriber with no subscriptions has nothing to report.
    assert svc.fup_summary(db_session, str(subscriber.id)) is None


def test_fup_summary_none_db_guard():
    # Endpoint passes db=None in some unit paths; must not blow up.
    assert svc.fup_summary(None, str(uuid.uuid4())) is None


def test_fup_summary_full_speed_when_no_state(db_session, subscriber, subscription):
    out = svc.fup_summary(db_session, str(subscriber.id))
    assert out["status"] == "full_speed"
    assert out["is_reduced"] is False
    # No FUP policy on the offer → no headroom/policy context to show.
    assert out["threshold_gb"] is None
    assert out["policy_summary"] is None


def _add_throttle_rule(db_session, subscription, threshold_gb=100):
    from app.models.fup import FupAction, FupConsumptionPeriod, FupDataUnit, FupRule
    from app.services.fup import fup_policies

    policy = fup_policies.get_or_create(db_session, str(subscription.offer_id))
    rule = FupRule(
        policy_id=policy.id,
        name=f"Monthly {threshold_gb}GB cap",
        consumption_period=FupConsumptionPeriod.monthly,
        threshold_amount=threshold_gb,
        threshold_unit=FupDataUnit.gb,
        action=FupAction.reduce_speed,
        speed_reduction_percent=75,
    )
    db_session.add(rule)
    db_session.commit()
    return rule


def _put_bucket(db_session, subscription, used_gb):
    from datetime import timedelta
    from decimal import Decimal

    from app.models.usage import QuotaBucket

    now = datetime.now(UTC)
    bucket = QuotaBucket(
        subscription_id=subscription.id,
        period_start=now - timedelta(days=5),
        period_end=now + timedelta(days=25),
        included_gb=Decimal("100"),
        used_gb=Decimal(str(used_gb)),
    )
    db_session.add(bucket)
    db_session.commit()
    return bucket


def test_fup_summary_healthy_shows_policy_terms_and_headroom(
    db_session, subscriber, subscription
):
    _add_throttle_rule(db_session, subscription, threshold_gb=100)
    _put_bucket(db_session, subscription, used_gb=40)

    out = svc.fup_summary(db_session, str(subscriber.id))
    assert out["status"] == "full_speed"
    assert out["threshold_gb"] == 100.0
    assert out["used_gb"] == 40.0
    assert out["gb_until_throttle"] == 60.0
    assert out["policy_summary"] == "Speed reduces to 25% after 100 GB each month"


def test_fup_summary_approaching_before_enforcement(
    db_session, subscriber, subscription
):
    _add_throttle_rule(db_session, subscription, threshold_gb=100)
    _put_bucket(db_session, subscription, used_gb=85)

    out = svc.fup_summary(db_session, str(subscriber.id))
    assert out["status"] == "approaching"
    assert out["is_reduced"] is False
    assert out["gb_until_throttle"] == 15.0
    assert "until it applies" in out["summary"]


def test_fup_summary_throttled_with_plain_language(
    db_session, subscriber, subscription
):
    from app.models.fup import FupAction, FupConsumptionPeriod, FupDataUnit, FupRule
    from app.models.fup_state import FupActionStatus
    from app.services.fup import fup_policies
    from app.services.fup_state import fup_state

    policy = fup_policies.get_or_create(db_session, str(subscription.offer_id))
    rule = FupRule(
        policy_id=policy.id,
        name="Monthly 100GB cap",
        consumption_period=FupConsumptionPeriod.monthly,
        threshold_amount=100,
        threshold_unit=FupDataUnit.gb,
        action=FupAction.reduce_speed,
        speed_reduction_percent=75,
    )
    db_session.add(rule)
    db_session.commit()

    fup_state.apply_action(
        db_session,
        str(subscription.id),
        offer_id=str(subscription.offer_id),
        rule_id=str(rule.id),
        action_status=FupActionStatus.throttled,
        speed_reduction_percent=75.0,
        cap_resets_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db_session.commit()

    out = svc.fup_summary(db_session, str(subscriber.id))
    assert out["status"] == "throttled"
    assert out["is_reduced"] is True
    assert out["speed_reduction_percent"] == 75.0
    assert out["active_rule_name"] == "Monthly 100GB cap"
    assert out["summary"] == "Speed reduced to 25% after 100 GB this month"


def test_usage_summary_403_for_non_subscriber():
    principal = {"principal_type": "system_user", "subscriber_id": str(uuid.uuid4())}
    with pytest.raises(HTTPException) as exc:
        _run_async(
            me_api.my_usage_summary(period="today", db=None, principal=principal)
        )
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
    _run_async(me_api.my_usage_summary(period="week", db=None, principal=principal))
    assert captured["subscriber_id"] == principal["subscriber_id"]
    assert captured["period"] == "week"


def test_yesterday_window_is_the_full_prior_local_day(db_session, subscriber):
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "yesterday", now=now)
    )
    assert out["period"] == "yesterday"
    # End is local midnight (UTC instant), start exactly one day earlier.
    assert out["end"] - out["start"] == timedelta(days=1)
    assert out["end"] <= now
    assert out["bucket"] == "hour"


def test_yesterday_does_not_fall_back_to_unbounded_sessions(
    db_session, subscriber, subscription
):
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    # A session TODAY must not leak into yesterday's comparison figure via the
    # sessions fallback (_session_octets has no upper bound).
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="today-2",
            status_type=AccountingStatus.stop,
            session_start=now - timedelta(hours=1),
            session_end=now,
            input_octets=4000,
            output_octets=1000,
        )
    )
    db_session.commit()

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "yesterday", now=now)
    )
    assert out["total_bytes"] == 0
    assert out["total_source"] == "samples"
