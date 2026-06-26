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

    # "lifetime" now: with no daily-rollup rows it is exactly the session total.
    assert out["total_source"] == "lifetime"
    assert out["is_authoritative"] is True
    assert out["bucket"] is None
    assert out["series"] == []
    assert out["total_bytes"] == 1000 + 500 + 2000 + 300  # includes active


def test_all_combines_daily_rollup_with_post_cutoff_sessions(
    db_session, subscriber, subscription
):
    """Lifetime 'all' = full daily rollup + only the sessions AFTER the rollup's
    last day, so the overlap between the two backfills isn't double-counted."""
    from datetime import date

    from app.models.usage import SubscriberDailyUsage

    # Daily rollup: 2 days in early 2020 (pre-session history), 3 GB total.
    for i, (up, down) in enumerate([(1_000, 2_000), (0, 3_000)]):
        db_session.add(
            SubscriberDailyUsage(
                subscription_id=subscription.id,
                splynx_service_id=4000 + i,
                usage_date=date(2020, 1, 1 + i),
                upload_bytes=up,
                download_bytes=down,
            )
        )
    # An OVERLAP session inside the rollup window — must NOT be added again.
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="overlap",
            status_type=AccountingStatus.stop,
            session_start=datetime(2020, 1, 1, 6, 0, tzinfo=UTC),
            input_octets=9_999,
            output_octets=9_999,
        )
    )
    # A session AFTER the rollup's last day (live post-cutover) — counted.
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="post",
            status_type=AccountingStatus.stop,
            session_start=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
            input_octets=500,
            output_octets=700,
        )
    )
    db_session.commit()

    now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "all", now=now)
    )

    # 6000 (daily) + 1200 (post-cutoff session); the 2020 overlap session excluded.
    assert out["total_bytes"] == 6_000 + 1_200
    assert out["total_source"] == "lifetime"
    assert out["start"].date() == date(2020, 1, 1)  # earliest = daily rollup start


def test_cycle_sums_session_octets_over_window(
    db_session, subscriber, subscription, monkeypatch
):
    """Cycle total = RADIUS session octets over the billing-cycle window — the
    rated bucket only defines the window, not the total."""

    async def _no_vm(db, sub_ids, start, end):
        return []

    monkeypatch.setattr(svc, "_vm_points", _no_vm)
    now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    # Bucket defines the window; its used_gb is irrelevant to the total now.
    db_session.add(
        QuotaBucket(
            subscription_id=subscription.id,
            period_start=now - timedelta(days=10),
            period_end=now + timedelta(days=20),
            included_gb=500,
            used_gb=2,
        )
    )
    # In-window session (counted) + a pre-window session (excluded).
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="in-window",
            status_type=AccountingStatus.stop,
            session_start=now - timedelta(days=3),
            input_octets=3000,
            output_octets=2000,
        )
    )
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="pre-window",
            status_type=AccountingStatus.stop,
            session_start=now - timedelta(days=40),
            input_octets=9999,
            output_octets=9999,
        )
    )
    db_session.commit()

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "cycle", now=now)
    )

    assert out["total_source"] == "sessions"
    assert out["is_authoritative"] is True
    assert out["total_bytes"] == 5000  # only the in-window session
    assert out["bucket"] == "day"


def test_cycle_unlimited_uses_session_octets(
    db_session, subscriber, subscription, monkeypatch
):
    """An unlimited/unmetered plan has a rated bucket with used_gb=0; the cycle
    total comes from session octets over the window, not the (0) quota."""

    async def _no_vm(db, sub_ids, start, end):
        return []

    monkeypatch.setattr(svc, "_vm_points", _no_vm)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    db_session.add(
        QuotaBucket(
            subscription_id=subscription.id,
            period_start=now - timedelta(days=10),
            period_end=now + timedelta(days=20),
            included_gb=None,  # unlimited
            used_gb=0,
        )
    )
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id="cyc-1",
            status_type=AccountingStatus.interim,
            session_start=now - timedelta(days=1),
            session_end=None,
            input_octets=3_000,
            output_octets=2_000,
        )
    )
    db_session.commit()

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "cycle", now=now)
    )

    assert out["total_bytes"] == 5_000
    assert out["total_source"] == "sessions"
    assert out["is_authoritative"] is True


def test_cycle_includes_peak_over_window(
    db_session, subscriber, subscription, monkeypatch
):
    """The cycle summary carries exact peak (download/upload) over the window."""

    async def _no_vm(db, sub_ids, start, end):
        return []

    async def _peak(db, sub_ids, start, end):
        return (123_000_000.0, 45_000_000.0)  # 123 / 45 Mbps

    monkeypatch.setattr(svc, "_vm_points", _no_vm)
    monkeypatch.setattr(svc, "_peak_directions", _peak)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    db_session.add(
        QuotaBucket(
            subscription_id=subscription.id,
            period_start=now - timedelta(days=10),
            period_end=now + timedelta(days=20),
            included_gb=500,
            used_gb=40,
        )
    )
    db_session.commit()

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "cycle", now=now)
    )
    assert out["peak_download_bps"] == 123_000_000.0
    assert out["peak_upload_bps"] == 45_000_000.0


def test_cycle_peak_falls_back_to_series_when_metrics_peak_empty(
    db_session, subscriber, subscription, monkeypatch
):
    """When the exact metrics-store peak is empty (VictoriaMetrics holds no data
    — e.g. on-demand polling), the cycle summary still reports a peak derived
    from the throughput series, which resolves VM->Postgres raw samples."""

    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    async def _points(db, sub_ids, start, end):
        # (ts, rx_bps, tx_bps). to_subscriber_directions maps tx->download.
        return [
            (now - timedelta(hours=2), 1_000_000.0, 8_000_000.0),
            (now - timedelta(hours=1), 2_500_000.0, 20_000_000.0),  # peaks
        ]

    async def _peak_none(db, sub_ids, start, end):
        return (None, None)

    monkeypatch.setattr(svc, "_vm_points", _points)
    monkeypatch.setattr(svc, "_peak_directions", _peak_none)
    db_session.add(
        QuotaBucket(
            subscription_id=subscription.id,
            period_start=now - timedelta(days=10),
            period_end=now + timedelta(days=20),
            included_gb=500,
            used_gb=40,
        )
    )
    db_session.commit()

    out = _run_async(
        svc.get_usage_summary(db_session, str(subscriber.id), "cycle", now=now)
    )
    # download = max tx (20 Mbps), upload = max rx (2.5 Mbps)
    assert out["peak_download_bps"] == 20_000_000.0
    assert out["peak_upload_bps"] == 2_500_000.0


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
    assert _run_async(svc.fup_summary(db_session, str(subscriber.id))) is None


def test_fup_summary_none_db_guard():
    # Endpoint passes db=None in some unit paths; must not blow up.
    assert _run_async(svc.fup_summary(None, str(uuid.uuid4()))) is None


def test_fup_summary_full_speed_when_no_state(db_session, subscriber, subscription):
    out = _run_async(svc.fup_summary(db_session, str(subscriber.id)))
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

    out = _run_async(svc.fup_summary(db_session, str(subscriber.id)))
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

    out = _run_async(svc.fup_summary(db_session, str(subscriber.id)))
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

    out = _run_async(svc.fup_summary(db_session, str(subscriber.id)))
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


# --- daily usage history (Splynx traffic_counter backfill) -----------------


def test_daily_usage_history_sums_and_scopes(db_session, subscriber, subscription):
    from datetime import date

    from app.models.usage import SubscriberDailyUsage

    for i, (up, down) in enumerate([(100, 900), (200, 800)]):
        db_session.add(
            SubscriberDailyUsage(
                subscription_id=subscription.id,
                splynx_service_id=5000 + i,
                usage_date=date(2020, 1, 1 + i),
                upload_bytes=up,
                download_bytes=down,
            )
        )
    # A row for someone else's subscription must not leak into the caller's total.
    db_session.add(
        SubscriberDailyUsage(
            subscription_id=None,
            splynx_service_id=9999,
            usage_date=date(2020, 1, 1),
            upload_bytes=10**9,
            download_bytes=10**9,
        )
    )
    db_session.commit()

    out = svc.get_daily_usage_history(db_session, str(subscriber.id), days=3660)
    assert out["total_upload_bytes"] == 300
    assert out["total_download_bytes"] == 1700
    assert out["total_bytes"] == 2000
    assert len(out["points"]) == 2
    assert out["points"][0]["date"] == date(2020, 1, 1)
    assert out["points"][0]["total_bytes"] == 1000


def test_daily_usage_history_empty_for_no_subscriptions(db_session, subscriber):
    out = svc.get_daily_usage_history(db_session, str(subscriber.id), days=30)
    assert out["points"] == []
    assert out["total_bytes"] == 0
