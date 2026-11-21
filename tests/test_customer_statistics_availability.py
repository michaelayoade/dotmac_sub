"""Staff Statistics uses the existing SLA evidence and outage interval engine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.models.catalog import Subscription
from app.models.network_monitoring import CustomerOutageInterval
from app.services import customer_service_level as sla
from app.services.network.radius_sessions import (
    AccountingCoverageInterval,
    AccountingCoveragePeriodHistory,
)
from app.services.sla_interval_algebra import Span
from app.services.subscription_lifecycle_history import (
    ActiveWindow,
    LifecyclePeriodHistory,
)

START = datetime(2026, 11, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def _accounting(subscription_id, *windows):
    return AccountingCoveragePeriodHistory(
        subscription_id=subscription_id,
        period_start=START,
        period_end=END,
        intervals=tuple(
            AccountingCoverageInterval(
                subscription_id=subscription_id,
                source_id=uuid4(),
                starts_at=start,
                ends_at=end,
            )
            for start, end in windows
        ),
        complete=True,
        issues=(),
    )


def _outage(subscription_id, start, end, *, quality="exact"):
    return CustomerOutageInterval(
        id=uuid4(),
        incident_id=uuid4(),
        subscription_id=subscription_id,
        state="confirmed_unavailable",
        quality=quality,
        started_at=start,
        ended_at=end,
        finalized_at=end,
        scope_revision_sequence=1,
        idempotency_key=f"test:{uuid4()}",
    )


def test_provider_outage_and_customer_offline_overlap_once(monkeypatch):
    subscription_id = uuid4()
    connected = (
        (START, START + timedelta(hours=11, minutes=15)),
        (START + timedelta(hours=12, minutes=30), END),
    )
    monkeypatch.setattr(
        sla,
        "accounting_coverage_for_period",
        lambda *_args, **_kwargs: _accounting(subscription_id, *connected),
    )
    monkeypatch.setattr(
        sla,
        "intervals_for_subscription",
        lambda *_args, **_kwargs: [
            _outage(
                subscription_id,
                START + timedelta(hours=10),
                START + timedelta(hours=12),
            )
        ],
    )

    evidence = sla._availability_evidence(
        None,
        subscription_id,
        period_start=START,
        period_end=END,
        expected=(Span(START, END),),
        contractual_exclusions=False,
    )
    assert sla.total_seconds(evidence.downtime) == 2 * 3600
    assert sla.total_seconds(evidence.connected) == 22 * 3600 + 45 * 60
    assert sla.total_seconds(evidence.unknown) == 30 * 60
    assert (
        sla.total_seconds(sla.subtract(evidence.connected, evidence.downtime))
        == 21 * 3600 + 30 * 60
    )


def test_two_overlapping_incidents_count_two_and_a_half_hours(monkeypatch):
    subscription_id = uuid4()
    monkeypatch.setattr(
        sla,
        "accounting_coverage_for_period",
        lambda *_args, **_kwargs: _accounting(subscription_id),
    )
    monkeypatch.setattr(
        sla,
        "intervals_for_subscription",
        lambda *_args, **_kwargs: [
            _outage(
                subscription_id,
                START + timedelta(hours=10),
                START + timedelta(hours=12),
            ),
            _outage(
                subscription_id,
                START + timedelta(hours=11, minutes=15),
                START + timedelta(hours=12, minutes=30),
            ),
            _outage(
                subscription_id,
                START + timedelta(hours=14),
                START + timedelta(hours=15),
                quality="estimated",
            ),
        ],
    )
    evidence = sla._availability_evidence(
        None,
        subscription_id,
        period_start=START,
        period_end=END,
        expected=(Span(START, END),),
        contractual_exclusions=False,
    )
    assert sla.total_seconds(evidence.downtime) == 2 * 3600 + 30 * 60
    assert sla.total_seconds(evidence.unknown) == 21 * 3600 + 30 * 60


def test_customer_offline_without_confirmed_incident_is_not_provider_downtime(
    monkeypatch,
):
    subscription_id = uuid4()
    monkeypatch.setattr(
        sla,
        "accounting_coverage_for_period",
        lambda *_args, **_kwargs: _accounting(
            subscription_id,
            (START, START + timedelta(hours=10)),
            (START + timedelta(hours=12), END),
        ),
    )
    # A complaint ticket or a powered-off device cannot enter this ledger read.
    monkeypatch.setattr(sla, "intervals_for_subscription", lambda *_a, **_k: [])
    evidence = sla._availability_evidence(
        None,
        subscription_id,
        period_start=START,
        period_end=END,
        expected=(Span(START, END),),
        contractual_exclusions=False,
    )
    assert sla.total_seconds(evidence.downtime) == 0
    assert sla.total_seconds(evidence.unknown) == 2 * 3600


def test_calendar_windows_include_custom_end_date():
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    customer_id = uuid4()
    zone = ZoneInfo("Africa/Lagos")
    current = sla.statistics_period_bounds(
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=customer_id,
            period=sla.StatisticsPeriod.current,
            evaluated_at=now,
        )
    )
    last = sla.statistics_period_bounds(
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=customer_id,
            period=sla.StatisticsPeriod.last,
            evaluated_at=now,
        )
    )
    custom = sla.statistics_period_bounds(
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=customer_id,
            period=sla.StatisticsPeriod.custom,
            start_date=date(2026, 11, 10),
            end_date=date(2026, 11, 12),
            evaluated_at=now,
        )
    )
    assert current == (datetime(2026, 11, 1, tzinfo=zone).astimezone(UTC), now)
    assert last == (
        datetime(2026, 10, 1, tzinfo=zone).astimezone(UTC),
        datetime(2026, 11, 1, tzinfo=zone).astimezone(UTC),
    )
    assert custom == (
        datetime(2026, 11, 10, tzinfo=zone).astimezone(UTC),
        datetime(2026, 11, 13, tzinfo=zone).astimezone(UTC),
    )


def test_multiple_services_remain_separate_and_suspended_time_is_excluded(
    db_session, subscriber, subscription, monkeypatch
):
    second = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
    )
    db_session.add(second)
    db_session.flush()

    def lifecycle(_db, subscription_id, *, period_start, period_end):
        active = (
            (ActiveWindow(period_start, period_start + timedelta(hours=10)),)
            if subscription_id == subscription.id
            else (ActiveWindow(period_start, period_end),)
        )
        return LifecyclePeriodHistory(
            subscription_id=subscription_id,
            period_start=period_start,
            period_end=period_end,
            active_windows=active,
            complete=True,
            coverage_start=period_start,
            supporting_evidence_ids=(uuid4(),),
            issues=(),
        )

    monkeypatch.setattr(sla, "lifecycle_history_for_period", lifecycle)
    monkeypatch.setattr(
        sla,
        "accounting_coverage_for_period",
        lambda _db, subscription_id, *, period_start, period_end: (
            AccountingCoveragePeriodHistory(
                subscription_id=subscription_id,
                period_start=period_start,
                period_end=period_end,
                intervals=(),
                complete=True,
                issues=(),
            )
        ),
    )
    monkeypatch.setattr(sla, "intervals_for_subscription", lambda *_a, **_k: [])
    result = sla.query_customer_statistics_availability(
        db_session,
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=subscriber.id,
            period=sla.StatisticsPeriod.custom,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 1),
            evaluated_at=datetime(2026, 11, 2, tzinfo=UTC),
        ),
    )
    assert len(result.services) == 2
    by_id = {item.subscription_id: item for item in result.services}
    assert by_id[subscription.id].expected_seconds == 10 * 3600
    assert by_id[second.id].expected_seconds == 24 * 3600
    assert all(item.confirmed_downtime_seconds == 0 for item in result.services)
    assert all(item.provisional for item in result.services)
