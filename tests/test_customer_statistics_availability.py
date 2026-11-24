"""Staff Statistics uses the existing SLA evidence and outage interval engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.billing_contract import (
    CadenceAlignment,
    CollectionTiming,
    IntervalUnit,
    RateBasis,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.network_monitoring import CustomerOutageInterval
from app.models.subscriber import SubscriberStatus
from app.services import customer_service_level as sla
from app.services.billing.cadence import BillingCadence
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


def _complete_availability_evidence(monkeypatch):
    def lifecycle(_db, subscription_id, *, period_start, period_end):
        return LifecyclePeriodHistory(
            subscription_id=subscription_id,
            period_start=period_start,
            period_end=period_end,
            active_windows=(ActiveWindow(period_start, period_end),),
            complete=True,
            coverage_start=period_start,
            supporting_evidence_ids=(uuid4(),),
            issues=(),
        )

    def accounting(_db, subscription_id, *, period_start, period_end):
        return AccountingCoveragePeriodHistory(
            subscription_id=subscription_id,
            period_start=period_start,
            period_end=period_end,
            intervals=(),
            complete=True,
            issues=(),
        )

    monkeypatch.setattr(sla, "lifecycle_history_for_period", lifecycle)
    monkeypatch.setattr(sla, "accounting_coverage_for_period", accounting)
    monkeypatch.setattr(sla, "intervals_for_subscription", lambda *_a, **_k: [])


class _SubscriptionRows:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_clauses):
        return self

    def all(self):
        return self.rows


def _selected_service_stub(monkeypatch):
    def project(_db, subscription, period, *, evaluated_at):
        return sla.SubscriptionStatisticsAvailability(
            subscription_id=subscription.id,
            label=subscription.offer.name,
            period_start=period.start,
            period_end=min(period.end, evaluated_at),
            expected_seconds=3600,
            connected_seconds=3600,
            confirmed_downtime_seconds=0,
            unknown_seconds=0,
            uptime_lower_seconds=3600,
            uptime_upper_seconds=3600,
            provisional=False,
            reason=None,
        )

    monkeypatch.setattr(sla, "_statistics_period_availability", project)


def test_one_active_service_wins_without_reading_paused_history(monkeypatch):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    active_id, paused_id = uuid4(), uuid4()
    active = SimpleNamespace(
        id=active_id,
        offer=SimpleNamespace(name="Active service"),
        status=SubscriptionStatus.active,
        start_at=START,
        created_at=START,
    )
    paused = SimpleNamespace(
        id=paused_id,
        offer=SimpleNamespace(name="Paused service"),
        status=SubscriptionStatus.paused,
        start_at=START + timedelta(days=1),
        created_at=START + timedelta(days=1),
    )
    db = SimpleNamespace(query=lambda _model: _SubscriptionRows([paused, active]))
    monkeypatch.setattr(
        sla,
        "_statistics_service_periods",
        lambda _db, service, *, now, include_history: (
            Span(START, now + timedelta(days=1)),
        ),
    )
    monkeypatch.setattr(
        sla,
        "_statistics_last_active_end",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paused history must not be read when active service exists")
        ),
    )
    _selected_service_stub(monkeypatch)

    result = sla.query_customer_statistics_availability(
        db,
        sla.CustomerStatisticsAvailabilityQuery(customer_id=uuid4(), evaluated_at=now),
    )

    assert result.service is not None
    assert result.service.subscription_id == active_id
    assert result.selected_status is SubscriptionStatus.active


def test_paused_or_suspended_fallback_uses_latest_proven_active_time(monkeypatch):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    paused_id, suspended_id = uuid4(), uuid4()
    paused = SimpleNamespace(
        id=paused_id,
        offer=SimpleNamespace(name="Paused service"),
        status=SubscriptionStatus.paused,
        start_at=START,
        created_at=START,
    )
    suspended = SimpleNamespace(
        id=suspended_id,
        offer=SimpleNamespace(name="Suspended service"),
        status=SubscriptionStatus.suspended,
        start_at=START,
        created_at=START,
    )
    db = SimpleNamespace(query=lambda _model: _SubscriptionRows([paused, suspended]))
    last_active = {
        paused_id: now - timedelta(days=2),
        suspended_id: now - timedelta(days=8),
    }
    monkeypatch.setattr(
        sla,
        "_statistics_last_active_end",
        lambda _db, service, *, now: last_active[service.id],
    )
    period_reads = []

    def periods(_db, service, *, now, include_history):
        period_reads.append((service.id, include_history))
        return (Span(START, now + timedelta(days=10)),)

    monkeypatch.setattr(sla, "_statistics_service_periods", periods)
    _selected_service_stub(monkeypatch)

    result = sla.query_customer_statistics_availability(
        db,
        sla.CustomerStatisticsAvailabilityQuery(customer_id=uuid4(), evaluated_at=now),
    )

    assert result.service is not None
    assert result.service.subscription_id == paused_id
    assert result.service.period_end == last_active[paused_id]
    assert period_reads == [(paused_id, True)]


def test_active_service_without_verified_period_does_not_show_paused_service(
    monkeypatch,
):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    active = SimpleNamespace(
        id=uuid4(),
        offer=SimpleNamespace(name="Active service"),
        status=SubscriptionStatus.active,
        start_at=START,
        created_at=START,
    )
    paused = SimpleNamespace(
        id=uuid4(),
        offer=SimpleNamespace(name="Paused service"),
        status=SubscriptionStatus.paused,
        start_at=START,
        created_at=START,
    )
    db = SimpleNamespace(query=lambda _model: _SubscriptionRows([active, paused]))
    monkeypatch.setattr(
        sla, "_statistics_service_periods", lambda *_args, **_kwargs: ()
    )

    result = sla.query_customer_statistics_availability(
        db,
        sla.CustomerStatisticsAvailabilityQuery(customer_id=uuid4(), evaluated_at=now),
    )

    assert result.service is None
    assert result.selected_status is SubscriptionStatus.active
    assert result.selected_label.startswith("Active service")
    assert "no verified service period" in result.reason


def test_paused_fallback_rejects_history_still_claiming_active_now(monkeypatch):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    service_id = uuid4()
    subscription = SimpleNamespace(
        id=service_id,
        status=SubscriptionStatus.paused,
        start_at=START,
        created_at=START,
    )
    monkeypatch.setattr(
        sla,
        "lifecycle_history_for_period",
        lambda _db, subscription_id, *, period_start, period_end: (
            LifecyclePeriodHistory(
                subscription_id=subscription_id,
                period_start=period_start,
                period_end=period_end,
                active_windows=(ActiveWindow(period_start, now),),
                complete=True,
                coverage_start=period_start,
                supporting_evidence_ids=(uuid4(),),
                issues=(),
            )
        ),
    )

    assert sla._statistics_last_active_end(None, subscription, now=now) is None


def test_paused_fallback_rejects_incomplete_lifecycle_history(monkeypatch):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    subscription = SimpleNamespace(
        id=uuid4(),
        status=SubscriptionStatus.paused,
        start_at=START,
        created_at=START,
    )
    monkeypatch.setattr(
        sla,
        "lifecycle_history_for_period",
        lambda _db, subscription_id, *, period_start, period_end: (
            LifecyclePeriodHistory(
                subscription_id=subscription_id,
                period_start=period_start,
                period_end=period_end,
                active_windows=(ActiveWindow(period_start, now - timedelta(days=1)),),
                complete=False,
                coverage_start=period_start,
                supporting_evidence_ids=(uuid4(),),
                issues=("missing transition",),
            )
        ),
    )

    assert sla._statistics_last_active_end(None, subscription, now=now) is None


def test_no_current_service_has_an_explicit_empty_state():
    db = SimpleNamespace(query=lambda _model: _SubscriptionRows([]))

    result = sla.query_customer_statistics_availability(
        db,
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=uuid4(), evaluated_at=datetime(2026, 11, 20, tzinfo=UTC)
        ),
    )

    assert result.service is None
    assert result.selected_status is None
    assert "No active, paused, or suspended service" in result.reason


def test_overlapping_prepaid_intervals_do_not_become_one_service_period(monkeypatch):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    subscription = SimpleNamespace(
        id=uuid4(),
        billing_mode=BillingMode.prepaid,
        start_at=START,
        created_at=START,
    )
    monkeypatch.setattr(
        sla,
        "prepaid_coverage_history_for_period",
        lambda *_args, **_kwargs: SimpleNamespace(
            intervals=(
                SimpleNamespace(starts_at=START, ends_at=now + timedelta(days=2)),
                SimpleNamespace(
                    starts_at=START + timedelta(days=1),
                    ends_at=now + timedelta(days=3),
                ),
            )
        ),
    )

    assert (
        sla._statistics_service_periods(
            None, subscription, now=now, include_history=False
        )
        == ()
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


def test_active_service_wins_and_only_its_current_funded_period_is_measured(
    db_session, subscriber, subscription, monkeypatch
):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    current_start = datetime(2026, 10, 31, 12, tzinfo=UTC)
    previous_start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    first_start = datetime(2026, 9, 1, 12, tzinfo=UTC)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.start_at = first_start
    subscriber.status = SubscriberStatus.delinquent
    second = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        billing_mode=BillingMode.prepaid,
        status=SubscriptionStatus.canceled,
        start_at=previous_start,
    )
    paused = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        billing_mode=BillingMode.prepaid,
        status=SubscriptionStatus.paused,
        start_at=previous_start,
    )
    suspended = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        billing_mode=BillingMode.prepaid,
        status=SubscriptionStatus.suspended,
        start_at=previous_start,
    )
    db_session.add_all(
        [
            second,
            paused,
            suspended,
            ServiceEntitlement(
                account_id=subscriber.id,
                subscription_id=subscription.id,
                starts_at=first_start,
                ends_at=previous_start,
                status=ServiceEntitlementStatus.active,
            ),
            ServiceEntitlement(
                account_id=subscriber.id,
                subscription_id=subscription.id,
                starts_at=previous_start,
                ends_at=current_start,
                status=ServiceEntitlementStatus.active,
            ),
            ServiceEntitlement(
                account_id=subscriber.id,
                subscription_id=subscription.id,
                starts_at=current_start,
                ends_at=now + timedelta(days=28),
                status=ServiceEntitlementStatus.active,
            ),
        ]
    )
    db_session.flush()
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=second.id,
            starts_at=current_start,
            ends_at=now + timedelta(days=28),
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.flush()

    def lifecycle(_db, subscription_id, *, period_start, period_end):
        active_end = min(period_end, period_start + timedelta(hours=10))
        return LifecyclePeriodHistory(
            subscription_id=subscription_id,
            period_start=period_start,
            period_end=period_end,
            active_windows=(ActiveWindow(period_start, active_end),),
            complete=True,
            coverage_start=period_start,
            supporting_evidence_ids=(uuid4(),),
            issues=(),
        )

    monkeypatch.setattr(sla, "lifecycle_history_for_period", lifecycle)
    observed_ends = []

    def accounting(_db, subscription_id, *, period_start, period_end):
        observed_ends.append(period_end)
        return AccountingCoveragePeriodHistory(
            subscription_id=subscription_id,
            period_start=period_start,
            period_end=period_end,
            intervals=(),
            complete=True,
            issues=(),
        )

    monkeypatch.setattr(
        sla,
        "accounting_coverage_for_period",
        accounting,
    )
    monkeypatch.setattr(sla, "intervals_for_subscription", lambda *_a, **_k: [])
    coverage_reads = []
    original_coverage = sla.prepaid_coverage_history_for_period

    def coverage_read(db, service, **kwargs):
        coverage_reads.append((service.id, kwargs))
        return original_coverage(db, service, **kwargs)

    monkeypatch.setattr(sla, "prepaid_coverage_history_for_period", coverage_read)
    result = sla.query_customer_statistics_availability(
        db_session,
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=subscriber.id,
            evaluated_at=now,
        ),
    )
    assert result.service is not None
    assert result.service.subscription_id == subscription.id
    assert result.service.period_start == current_start
    assert result.service.period_end == now
    assert result.service.expected_seconds == 10 * 3600
    assert result.selected_status is SubscriptionStatus.active
    assert observed_ends == [now]
    assert coverage_reads == [
        (
            subscription.id,
            {
                "period_start": now,
                "period_end": now + timedelta(microseconds=1),
                "preserve_source_bounds": True,
            },
        )
    ]


def test_newest_verified_active_period_wins_when_two_services_are_active(
    db_session, subscriber, subscription, monkeypatch
):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    subscription.status = SubscriptionStatus.active
    subscription.start_at = START - timedelta(days=60)
    newer = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.active,
        start_at=START - timedelta(days=30),
    )
    db_session.add(newer)
    db_session.flush()
    _complete_availability_evidence(monkeypatch)
    monkeypatch.setattr(
        sla,
        "_statistics_service_periods",
        lambda _db, service, *, now, include_history: (
            Span(START + timedelta(days=1), now + timedelta(days=1))
            if service.id == newer.id
            else Span(START, now + timedelta(days=1)),
        ),
    )

    result = sla.query_customer_statistics_availability(
        db_session,
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=subscriber.id, evaluated_at=now
        ),
    )

    assert result.service is not None
    assert result.service.subscription_id == newer.id
    assert result.service.period_start == START + timedelta(days=1)
    assert result.service.period_end == now


def test_recently_active_paused_service_wins_over_older_suspended_service(
    db_session, subscriber, subscription, monkeypatch
):
    now = datetime(2026, 11, 20, tzinfo=UTC)
    paused_at = now - timedelta(days=2)
    suspended_at = now - timedelta(days=8)
    subscription.status = SubscriptionStatus.suspended
    subscription.start_at = START - timedelta(days=30)
    paused = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.paused,
        start_at=START - timedelta(days=10),
    )
    db_session.add(paused)
    db_session.flush()
    _complete_availability_evidence(monkeypatch)
    monkeypatch.setattr(
        sla,
        "_statistics_last_active_end",
        lambda _db, service, *, now: (
            paused_at if service.id == paused.id else suspended_at
        ),
    )
    period_reads = []

    def periods(_db, service, *, now, include_history):
        period_reads.append((service.id, include_history))
        return (Span(START, now + timedelta(days=10)),)

    monkeypatch.setattr(sla, "_statistics_service_periods", periods)
    result = sla.query_customer_statistics_availability(
        db_session,
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=subscriber.id, evaluated_at=now
        ),
    )

    assert result.service is not None
    assert result.service.subscription_id == paused.id
    assert result.service.period_start == START
    assert result.service.period_end == paused_at
    assert result.selected_status is SubscriptionStatus.paused
    assert period_reads == [(paused.id, True)]


def test_no_verified_active_period_is_unavailable_instead_of_using_old_service(
    db_session, subscriber, subscription, monkeypatch
):
    subscription.status = SubscriptionStatus.paused
    db_session.flush()
    monkeypatch.setattr(sla, "_statistics_last_active_end", lambda *_a, **_k: None)
    result = sla.query_customer_statistics_availability(
        db_session,
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=subscriber.id,
            evaluated_at=datetime(2026, 11, 20, tzinfo=UTC),
        ),
    )
    assert result.service is None
    assert result.selected_status is None
    assert "proven completed active period" in result.reason


def test_noncurrent_statuses_never_enter_availability_selection(
    db_session, subscriber, subscription
):
    other_statuses = (
        SubscriptionStatus.blocked,
        SubscriptionStatus.stopped,
        SubscriptionStatus.disabled,
        SubscriptionStatus.expired,
        SubscriptionStatus.canceled,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
    )
    db_session.add_all(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=subscription.offer_id,
            status=status,
        )
        for status in other_statuses
    )
    db_session.flush()

    result = sla.query_customer_statistics_availability(
        db_session,
        sla.CustomerStatisticsAvailabilityQuery(
            customer_id=subscriber.id,
            evaluated_at=datetime(2026, 11, 20, tzinfo=UTC),
        ),
    )

    assert result.service is None
    assert "No active, paused, or suspended service" in result.reason


def test_postpaid_periods_follow_contract_cadence(monkeypatch):
    subscription_id = uuid4()
    contract_start = datetime(2026, 9, 15, tzinfo=UTC)
    now = datetime(2026, 11, 20, tzinfo=UTC)
    version_id = uuid4()
    version = SimpleNamespace(starts_at=contract_start, ends_at=None)
    subscription = SimpleNamespace(
        id=subscription_id,
        billing_mode=BillingMode.postpaid,
        start_at=contract_start,
        created_at=contract_start,
    )
    cadence = BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=IntervalUnit.month,
        rate_quantity=Decimal("1"),
        service_interval_unit=IntervalUnit.month,
        service_interval_count=1,
        invoice_interval_unit=IntervalUnit.month,
        invoice_interval_count=1,
        collection_timing=CollectionTiming.arrears,
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name="Africa/Lagos",
    )
    monkeypatch.setattr(sla.BillingContracts, "cadence_of", lambda _version: cadence)
    entitlement_reads = []

    def entitlement(_db, *, subscription_id, period_start, period_end):
        entitlement_reads.append((period_start, period_end))
        return SimpleNamespace(
            complete=True,
            intervals=(
                SimpleNamespace(
                    contract_version_id=version_id,
                    starts_at=max(contract_start, period_start),
                    ends_at=period_end,
                ),
            ),
        )

    monkeypatch.setattr(
        sla,
        "postpaid_entitlement_history_for_period",
        entitlement,
    )
    db = SimpleNamespace(get=lambda _model, _id: version)

    periods = sla._statistics_service_periods(
        db, subscription, now=now, include_history=True
    )

    assert [period.start for period in periods] == [
        datetime(2026, 9, 15, tzinfo=UTC),
        datetime(2026, 10, 15, tzinfo=UTC),
        datetime(2026, 11, 15, tzinfo=UTC),
    ]
    assert periods[-1].end == datetime(2026, 12, 15, tzinfo=UTC)
    current = sla._statistics_service_periods(
        db, subscription, now=now, include_history=False
    )
    assert current == (Span(datetime(2026, 11, 15, tzinfo=UTC), periods[-1].end),)
    assert entitlement_reads == [
        (contract_start, now + timedelta(microseconds=1)),
        (now, now + timedelta(microseconds=1)),
    ]
