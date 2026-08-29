"""Cadence geometry for cancellation credits — the money contract.

Two live defects motivated this file, both in the inline period-start reverse
that ``billing_automation.generate_cancellation_credit`` used to carry instead
of asking the declared owner
(``service_intent.subscription_billing_cadence`` ->
``app.services.catalog.subscriptions``):

1. **Quarterly over-credit.** The inline ``if``/``elif`` chain had
   ``daily``/``weekly``/``annual`` branches and a bare ``else: # monthly``.
   ``quarterly`` fell through to monthly, so a cancelled quarterly
   subscription's unused fraction was measured against a one-month period
   instead of a three-month one. Measured on ``main`` ``df6f4daec``: a 30,000
   quarterly line cancelled with 30 of 90 days left credited 29,032.26 instead
   of 10,000.00 — **2.90x, 19,032.26 over-issued on a single cancellation**.
2. **29 February silent zero-credit.** The ``annual`` branch used
   ``period_start.replace(year=period_start.year - 1)``, which raises
   ``ValueError: day is out of range for month`` when ``next_billing_at`` is
   29 February. ``account_lifecycle.cancel_subscription`` wraps the call in a
   broad ``except``, so the customer silently received **no credit at all**.

Sensitivity is proved per defect, not in aggregate: two surgical mutations
reintroduce one defect each, and the tests assert the EXACT set of properties
that bites. A property that stops detecting its defect fails the build, and so
does one that starts firing on a cadence that was never broken.
"""

from __future__ import annotations

import importlib
from calendar import monthrange
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from dateutil.relativedelta import relativedelta

from app.models.catalog import BillingCycle
from app.services.catalog.subscriptions import (
    _CYCLE_PERIOD_LENGTH,
    billing_cycle_end,
    billing_cycle_start,
)

CycleStart = Callable[[datetime, BillingCycle], datetime]


def _owner_module():
    """The cadence-owner MODULE, not the ``Subscriptions()`` singleton.

    ``app/services/catalog/__init__.py`` binds ``subscriptions = Subscriptions()``,
    so ``from app.services.catalog import subscriptions`` hands back the service
    instance and shadows the submodule. Monkeypatching that instance silently
    patches nothing the production code reads.
    """
    module = importlib.import_module("app.services.catalog.subscriptions")
    assert hasattr(module, "_CYCLE_PERIOD_LENGTH"), (
        "resolved the service singleton, not the cadence-owner module"
    )
    return module


# ---------------------------------------------------------------------------
# The mutations. ``_legacy_billing_cycle_start`` is the deleted implementation
# verbatim; the other two isolate one defect each so the sensitivity proof can
# tell them apart.
# ---------------------------------------------------------------------------
def _legacy_billing_cycle_start(
    period_start: datetime, cycle: BillingCycle
) -> datetime:
    """Verbatim copy of the pre-fix inline reverse in ``billing_automation``."""
    if cycle == BillingCycle.daily:
        return period_start - timedelta(days=1)
    elif cycle == BillingCycle.weekly:
        return period_start - timedelta(weeks=1)
    elif cycle == BillingCycle.annual:
        return period_start.replace(year=period_start.year - 1)
    else:  # monthly
        month = period_start.month - 1 or 12
        year = period_start.year if period_start.month > 1 else period_start.year - 1
        day = min(period_start.day, monthrange(year, month)[1])
        return period_start.replace(year=year, month=month, day=day)


def _mutation_quarterly_falls_through(
    next_billing_at: datetime, cycle: BillingCycle
) -> datetime:
    """DEFECT 1 alone: a correct implementation with no ``quarterly`` branch."""
    if cycle is BillingCycle.quarterly:
        cycle = BillingCycle.monthly
    return billing_cycle_start(next_billing_at, cycle)


def _mutation_annual_uses_replace_year(
    next_billing_at: datetime, cycle: BillingCycle
) -> datetime:
    """DEFECT 2 alone: a correct implementation that year-replaces for ``annual``."""
    if cycle is BillingCycle.annual:
        return next_billing_at.replace(year=next_billing_at.year - 1)
    return billing_cycle_start(next_billing_at, cycle)


# ---------------------------------------------------------------------------
# Independent oracle: dateutil, not a second copy of our own arithmetic.
# ---------------------------------------------------------------------------
_ORACLE_DELTA = {
    BillingCycle.daily: relativedelta(days=1),
    BillingCycle.weekly: relativedelta(weeks=1),
    BillingCycle.monthly: relativedelta(months=1),
    BillingCycle.quarterly: relativedelta(months=3),
    BillingCycle.annual: relativedelta(years=1),
}


def _oracle_start(next_billing_at: datetime, cycle: BillingCycle) -> datetime:
    return next_billing_at - _ORACLE_DELTA[cycle]


def _unused_fraction(
    cycle_start: CycleStart,
    *,
    next_billing_at: datetime,
    now: datetime,
    cycle: BillingCycle,
) -> Decimal:
    """The exact ratio ``generate_cancellation_credit`` multiplies the line by."""
    period_start = cycle_start(next_billing_at, cycle)
    total = max((next_billing_at - period_start).total_seconds(), 1)
    unused = max((next_billing_at - now).total_seconds(), 0)
    return Decimal(str(unused)) / Decimal(str(total))


# ===========================================================================
# Properties. Each takes the implementation under test so it can be run
# against both the real one and each mutation.
# ===========================================================================
def prop_every_cadence_is_supported(cycle_start: CycleStart) -> None:
    """No cadence may fall through to a default bucket."""
    base = datetime(2026, 5, 15, 9, 30, tzinfo=UTC)
    for cycle in BillingCycle:
        assert cycle_start(base, cycle) == _oracle_start(base, cycle), cycle


def prop_quarterly_is_three_calendar_months(cycle_start: CycleStart) -> None:
    """DEFECT 1. A quarter is three calendar months, never one and never 90 days."""
    cases = [
        (datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC), 90),
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2025, 10, 1, tzinfo=UTC), 92),
        (datetime(2026, 3, 31, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC), 90),
        (datetime(2024, 5, 31, tzinfo=UTC), datetime(2024, 2, 29, tzinfo=UTC), 92),
        (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC), 91),
    ]
    for next_billing, expected, span_days in cases:
        actual = cycle_start(next_billing, BillingCycle.quarterly)
        assert actual == expected, f"{next_billing} -> {actual}, expected {expected}"
        assert (next_billing - actual).days == span_days


def prop_quarterly_credit_is_not_inflated(cycle_start: CycleStart) -> None:
    """DEFECT 1, in money. 30 of 90 days left on a 30,000 line is 10,000."""
    line = Decimal("30000.00")
    ratio = _unused_fraction(
        cycle_start,
        next_billing_at=datetime(2026, 4, 1, tzinfo=UTC),
        now=datetime(2026, 3, 2, tzinfo=UTC),
        cycle=BillingCycle.quarterly,
    )
    assert (line * ratio).quantize(Decimal("0.01")) == Decimal("10000.00")


def prop_quarterly_credit_capped_with_a_month_left(cycle_start: CycleStart) -> None:
    """A quarterly cancellation with one month left never refunds most of the quarter."""
    for month in range(1, 13):
        next_billing = datetime(2026, month, 1, tzinfo=UTC)
        now = next_billing - relativedelta(months=1)
        ratio = _unused_fraction(
            cycle_start,
            next_billing_at=next_billing,
            now=now,
            cycle=BillingCycle.quarterly,
        )
        assert ratio <= Decimal("0.35"), f"{next_billing}: refunded {ratio:.4f}"


def prop_annual_reverse_of_29_february(cycle_start: CycleStart) -> None:
    """DEFECT 2. 29 Feb minus one year is 28 Feb, not a ValueError."""
    for leap_day, expected in (
        (datetime(2024, 2, 29, tzinfo=UTC), datetime(2023, 2, 28, tzinfo=UTC)),
        (datetime(2028, 2, 29, tzinfo=UTC), datetime(2027, 2, 28, tzinfo=UTC)),
        (datetime(2020, 2, 29, tzinfo=UTC), datetime(2019, 2, 28, tzinfo=UTC)),
    ):
        assert cycle_start(leap_day, BillingCycle.annual) == expected


def prop_leap_day_credit_is_issued(cycle_start: CycleStart) -> None:
    """DEFECT 2, in money. The 29 Feb customer is owed a real, exact amount."""
    ratio = _unused_fraction(
        cycle_start,
        next_billing_at=datetime(2024, 2, 29, tzinfo=UTC),
        now=datetime(2024, 2, 20, tzinfo=UTC),
        cycle=BillingCycle.annual,
    )
    # 9 unused days of the 366-day period 2023-02-28 .. 2024-02-29.
    assert (Decimal("30000.00") * ratio).quantize(Decimal("0.01")) == Decimal("737.70")


def prop_february_both_directions(cycle_start: CycleStart) -> None:
    """Leap and non-leap February, into and out of the 29th."""
    d = datetime
    cases = [
        # into a leap February
        (d(2024, 3, 29, tzinfo=UTC), BillingCycle.monthly, d(2024, 2, 29, tzinfo=UTC)),
        (d(2024, 3, 31, tzinfo=UTC), BillingCycle.monthly, d(2024, 2, 29, tzinfo=UTC)),
        # into a non-leap February
        (d(2025, 3, 29, tzinfo=UTC), BillingCycle.monthly, d(2025, 2, 28, tzinfo=UTC)),
        (d(2025, 3, 31, tzinfo=UTC), BillingCycle.monthly, d(2025, 2, 28, tzinfo=UTC)),
        # out of 29 February
        (d(2024, 2, 29, tzinfo=UTC), BillingCycle.monthly, d(2024, 1, 29, tzinfo=UTC)),
        (
            d(2024, 2, 29, tzinfo=UTC),
            BillingCycle.quarterly,
            d(2023, 11, 29, tzinfo=UTC),
        ),
        (d(2024, 2, 29, tzinfo=UTC), BillingCycle.annual, d(2023, 2, 28, tzinfo=UTC)),
        # a leap-year target from a leap-year source stays on the 29th
        (
            d(2024, 5, 29, tzinfo=UTC),
            BillingCycle.quarterly,
            d(2024, 2, 29, tzinfo=UTC),
        ),
    ]
    for next_billing, cycle, expected in cases:
        assert cycle_start(next_billing, cycle) == expected, (next_billing, cycle)


def prop_month_end_transitions_clamp(cycle_start: CycleStart) -> None:
    """A 31st anchor lands on the last day of a shorter month, never overflows."""
    d = datetime
    cases = [
        (d(2026, 5, 31, tzinfo=UTC), BillingCycle.monthly, d(2026, 4, 30, tzinfo=UTC)),
        (d(2026, 7, 31, tzinfo=UTC), BillingCycle.monthly, d(2026, 6, 30, tzinfo=UTC)),
        (
            d(2026, 12, 31, tzinfo=UTC),
            BillingCycle.monthly,
            d(2026, 11, 30, tzinfo=UTC),
        ),
        (d(2026, 1, 31, tzinfo=UTC), BillingCycle.monthly, d(2025, 12, 31, tzinfo=UTC)),
        (
            d(2026, 5, 31, tzinfo=UTC),
            BillingCycle.quarterly,
            d(2026, 2, 28, tzinfo=UTC),
        ),
        (
            d(2026, 8, 31, tzinfo=UTC),
            BillingCycle.quarterly,
            d(2026, 5, 31, tzinfo=UTC),
        ),
    ]
    for next_billing, cycle, expected in cases:
        actual = cycle_start(next_billing, cycle)
        assert actual == expected, (next_billing, cycle, actual)
        assert actual.day <= monthrange(actual.year, actual.month)[1]


def prop_clock_is_preserved(cycle_start: CycleStart) -> None:
    """Cadence moves calendar dates; it never rounds the clock."""
    anchor = datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
    for cycle in BillingCycle:
        got = cycle_start(anchor, cycle)
        assert (got.hour, got.minute, got.second, got.microsecond) == (0, 0, 0, 1)
    late = datetime(2026, 3, 15, 23, 59, 59, 999999, tzinfo=UTC)
    for cycle in BillingCycle:
        got = cycle_start(late, cycle)
        assert (got.hour, got.minute, got.second, got.microsecond) == (
            23,
            59,
            59,
            999999,
        )


def prop_timezone_is_preserved(cycle_start: CycleStart) -> None:
    """Cadence is wall-clock calendar arithmetic in the anchor's own zone."""
    lagos = ZoneInfo("Africa/Lagos")
    anchor = datetime(2026, 4, 1, 0, 30, tzinfo=lagos)
    got = cycle_start(anchor, BillingCycle.quarterly)
    assert got == datetime(2026, 1, 1, 0, 30, tzinfo=lagos)
    assert got.tzinfo is lagos

    # Across a DST transition the wall clock is preserved, so the absolute span
    # is NOT a whole number of days. A day-count approximation cannot do this.
    ny = ZoneInfo("America/New_York")
    dst_anchor = datetime(2026, 4, 1, 0, 0, tzinfo=ny)
    dst_start = cycle_start(dst_anchor, BillingCycle.quarterly)
    assert (dst_start.year, dst_start.month, dst_start.day, dst_start.hour) == (
        2026,
        1,
        1,
        0,
    )
    assert dst_anchor.utcoffset() != dst_start.utcoffset()
    # The wall clock is kept, so the ABSOLUTE span is 90 days minus the DST
    # hour. It has to be measured in UTC: CPython's datetime subtraction
    # ignores tzinfo entirely when both operands share the same tzinfo object,
    # so `dst_anchor - dst_start` reports a flat 90 days and would hide this.
    assert dst_anchor.astimezone(UTC) - dst_start.astimezone(UTC) == timedelta(
        days=90
    ) - timedelta(hours=1)

    naive = datetime(2026, 4, 1, 12, 0)
    assert cycle_start(naive, BillingCycle.quarterly).tzinfo is None


def prop_no_day_count_approximation(cycle_start: CycleStart) -> None:
    """Period spans must vary with the calendar; a fixed span is the bug shape."""
    quarter_spans = {
        (
            datetime(2026, m, 1, tzinfo=UTC)
            - cycle_start(datetime(2026, m, 1, tzinfo=UTC), BillingCycle.quarterly)
        ).days
        for m in range(1, 13)
    }
    assert quarter_spans == {89, 90, 91, 92}, quarter_spans
    annual_spans = {
        (
            datetime(y, 3, 1, tzinfo=UTC)
            - cycle_start(datetime(y, 3, 1, tzinfo=UTC), BillingCycle.annual)
        ).days
        for y in range(2020, 2033)
    }
    assert annual_spans == {365, 366}, annual_spans


def prop_matches_oracle_over_13_years(cycle_start: CycleStart) -> None:
    """Property sweep: every day 2020-01-01..2032-12-31 x every cadence."""
    day = datetime(2020, 1, 1, 6, 15, tzinfo=UTC)
    end = datetime(2033, 1, 1, tzinfo=UTC)
    checked = 0
    while day < end:
        for cycle in BillingCycle:
            assert cycle_start(day, cycle) == _oracle_start(day, cycle), (day, cycle)
            checked += 1
        day += timedelta(days=1)
    assert checked == 4749 * len(BillingCycle)


def prop_round_trip_identity(cycle_start: CycleStart) -> None:
    """Reverse-then-forward returns the anchor whenever no month-end clamp applies."""
    day = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2028, 1, 1, tzinfo=UTC)
    while day < end:
        for cycle in BillingCycle:
            start = cycle_start(day, cycle)
            if start.day == day.day:  # clamping is not invertible, by construction
                assert billing_cycle_end(start, cycle) == day, (day, cycle)
        day += timedelta(days=1)


PROPERTIES: dict[str, Callable[[CycleStart], None]] = {
    "every_cadence_is_supported": prop_every_cadence_is_supported,
    "quarterly_is_three_calendar_months": prop_quarterly_is_three_calendar_months,
    "quarterly_credit_is_not_inflated": prop_quarterly_credit_is_not_inflated,
    "quarterly_credit_capped_with_a_month_left": (
        prop_quarterly_credit_capped_with_a_month_left
    ),
    "annual_reverse_of_29_february": prop_annual_reverse_of_29_february,
    "leap_day_credit_is_issued": prop_leap_day_credit_is_issued,
    "february_both_directions": prop_february_both_directions,
    "month_end_transitions_clamp": prop_month_end_transitions_clamp,
    "clock_is_preserved": prop_clock_is_preserved,
    "timezone_is_preserved": prop_timezone_is_preserved,
    "no_day_count_approximation": prop_no_day_count_approximation,
    "matches_oracle_over_13_years": prop_matches_oracle_over_13_years,
    "round_trip_identity": prop_round_trip_identity,
}

# Exactly which properties each defect must trip. Two-directional: a property
# that stops detecting its defect fails here, and so does one that starts
# firing on a cadence the mutation left correct.
QUARTERLY_DEFECT_DETECTORS = frozenset(
    {
        "every_cadence_is_supported",
        "quarterly_is_three_calendar_months",
        "quarterly_credit_is_not_inflated",
        "quarterly_credit_capped_with_a_month_left",
        "february_both_directions",
        "month_end_transitions_clamp",
        "timezone_is_preserved",
        "no_day_count_approximation",
        "matches_oracle_over_13_years",
        "round_trip_identity",
    }
)

LEAP_DAY_DEFECT_DETECTORS = frozenset(
    {
        "annual_reverse_of_29_february",
        "leap_day_credit_is_issued",
        "february_both_directions",
        "matches_oracle_over_13_years",
        "round_trip_identity",
    }
)


def _biting_properties(mutation: CycleStart) -> set[str]:
    biting = set()
    for name, prop in PROPERTIES.items():
        try:
            prop(mutation)
        except (AssertionError, ValueError):
            biting.add(name)
    return biting


@pytest.mark.parametrize("name", sorted(PROPERTIES))
def test_billing_cycle_start_satisfies_the_cadence_contract(name: str) -> None:
    PROPERTIES[name](billing_cycle_start)


def test_removing_the_quarterly_branch_trips_exactly_its_detectors() -> None:
    """Sensitivity proof for DEFECT 1."""
    biting = _biting_properties(_mutation_quarterly_falls_through)
    assert biting == set(QUARTERLY_DEFECT_DETECTORS), {
        "stopped_detecting": sorted(QUARTERLY_DEFECT_DETECTORS - biting),
        "unexpectedly_detected": sorted(biting - QUARTERLY_DEFECT_DETECTORS),
    }


def test_year_replacement_for_annual_trips_exactly_its_detectors() -> None:
    """Sensitivity proof for DEFECT 2."""
    biting = _biting_properties(_mutation_annual_uses_replace_year)
    assert biting == set(LEAP_DAY_DEFECT_DETECTORS), {
        "stopped_detecting": sorted(LEAP_DAY_DEFECT_DETECTORS - biting),
        "unexpectedly_detected": sorted(biting - LEAP_DAY_DEFECT_DETECTORS),
    }


def test_the_deleted_implementation_trips_the_union_of_both() -> None:
    """The real pre-fix code carried both defects at once."""
    biting = _biting_properties(_legacy_billing_cycle_start)
    assert biting == set(QUARTERLY_DEFECT_DETECTORS | LEAP_DAY_DEFECT_DETECTORS)


def test_the_two_defects_are_named_not_merely_detected() -> None:
    """Pin the exact observed misbehaviour, so a rename cannot hide it."""
    # 1. quarterly silently became monthly ...
    anchor = datetime(2026, 4, 1, tzinfo=UTC)
    assert _legacy_billing_cycle_start(
        anchor, BillingCycle.quarterly
    ) == _legacy_billing_cycle_start(anchor, BillingCycle.monthly)
    # ... and over-credited 2.9x on a 30,000 line with 30 of 90 days left.
    legacy_credit = (
        Decimal("30000.00")
        * _unused_fraction(
            _legacy_billing_cycle_start,
            next_billing_at=anchor,
            now=datetime(2026, 3, 2, tzinfo=UTC),
            cycle=BillingCycle.quarterly,
        )
    ).quantize(Decimal("0.01"))
    assert legacy_credit == Decimal("29032.26")
    correct_credit = (
        Decimal("30000.00")
        * _unused_fraction(
            billing_cycle_start,
            next_billing_at=anchor,
            now=datetime(2026, 3, 2, tzinfo=UTC),
            cycle=BillingCycle.quarterly,
        )
    ).quantize(Decimal("0.01"))
    assert correct_credit == Decimal("10000.00")
    assert legacy_credit - correct_credit == Decimal("19032.26")

    # 2. the annual reverse of 29 February raised, and the caller swallowed it.
    with pytest.raises(ValueError, match="day is out of range for month"):
        _legacy_billing_cycle_start(
            datetime(2024, 2, 29, tzinfo=UTC), BillingCycle.annual
        )


# ===========================================================================
# Totality of the owner's table
# ===========================================================================
def test_every_billing_cycle_has_a_declared_period_length() -> None:
    """A new BillingCycle member must be declared, not silently billed monthly."""
    assert set(_CYCLE_PERIOD_LENGTH) == set(BillingCycle)
    for cycle, (months, days) in _CYCLE_PERIOD_LENGTH.items():
        assert (months > 0) ^ (days > 0), f"{cycle}: exactly one component is set"


def test_an_undeclared_cadence_fails_closed_instead_of_defaulting_to_monthly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sensitivity proof for the table itself: drop a row, the money path refuses."""
    owner = _owner_module()

    trimmed = dict(_CYCLE_PERIOD_LENGTH)
    del trimmed[BillingCycle.quarterly]
    monkeypatch.setattr(owner, "_CYCLE_PERIOD_LENGTH", trimmed)

    with pytest.raises(ValueError, match="No billing-period length is declared"):
        owner.billing_cycle_start(
            datetime(2026, 4, 1, tzinfo=UTC), BillingCycle.quarterly
        )
    assert owner.billing_cycle_start(
        datetime(2026, 4, 1, tzinfo=UTC), BillingCycle.monthly
    ) == datetime(2026, 3, 1, tzinfo=UTC)


def test_forward_and_reverse_read_the_same_table() -> None:
    """One table, two directions — they cannot drift apart."""
    anchor = datetime(2026, 6, 15, 8, 0, tzinfo=UTC)
    for cycle in BillingCycle:
        assert billing_cycle_start(billing_cycle_end(anchor, cycle), cycle) == anchor


# ===========================================================================
# The real money path: app.services.billing_automation.generate_cancellation_credit
#
# The properties above pin the geometry. These pin the CREDIT NOTE the customer
# actually receives, through the live function, and prove the assertions bite
# when the deleted implementation is injected back into the owner.
# ===========================================================================
_LINE_AMOUNT = Decimal("30000.00")


def _freeze_billing_clock(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Pin ``billing_automation``'s notion of now so credits are exact, not fuzzy."""
    from app.services import billing_automation

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return instant if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(billing_automation, "datetime", _FrozenDatetime)


def _bill_one_quarter(
    db_session, subscription, subscriber_account, *, cycle, next_billing_at
):
    """Give the subscription one issued invoice line on ``cycle``, and return it."""
    from app.models.billing import (
        Invoice,
        InvoiceLine,
        InvoiceStatus,
        TaxApplication,
        TaxRate,
    )
    from app.models.catalog import SubscriptionStatus
    from app.models.subscriber import AccountStatus

    subscription.status = SubscriptionStatus.active
    subscriber_account.status = AccountStatus.active
    # Subscription-owned cadence wins (SOT precedence), and the offer agrees so
    # the fallback cannot quietly supply a different one.
    subscription.billing_cycle = cycle
    if subscription.offer is not None:
        subscription.offer.billing_cycle = cycle
    subscription.next_billing_at = next_billing_at

    tax_rate = TaxRate(name="Cancellation VAT", code="VAT-CC", rate=Decimal("7.5000"))
    db_session.add(tax_rate)
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-CC-CADENCE",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=_LINE_AMOUNT,
        tax_total=Decimal("2250.00"),
        total=Decimal("32250.00"),
        balance_due=Decimal("32250.00"),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Plan",
            quantity=Decimal("1.000"),
            unit_price=_LINE_AMOUNT,
            amount=_LINE_AMOUNT,
            tax_rate_id=tax_rate.id,
            tax_application=TaxApplication.exclusive,
        )
    )
    db_session.commit()
    return invoice


def _issued_credit(db_session, subscriber_account):
    from app.models.billing import CreditNote

    return (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == subscriber_account.id)
        .first()
    )


class TestCancellationCreditMoney:
    """DEFECT 1 and DEFECT 2 through the live credit-note path."""

    def test_quarterly_cancellation_credits_the_quarter_not_the_month(
        self, db_session, subscription, subscriber_account, monkeypatch
    ):
        from app.services import billing_automation

        _bill_one_quarter(
            db_session,
            subscription,
            subscriber_account,
            cycle=BillingCycle.quarterly,
            next_billing_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        _freeze_billing_clock(monkeypatch, datetime(2026, 3, 2, tzinfo=UTC))

        billing_automation.generate_cancellation_credit(db_session, subscription)

        credit = _issued_credit(db_session, subscriber_account)
        assert credit is not None
        # 30 unused days of the 90-day quarter 2026-01-01 .. 2026-04-01.
        assert credit.subtotal == Decimal("10000.00")
        assert credit.total == credit.subtotal + credit.tax_total
        # The deleted implementation issued 29,032.26 here.
        assert credit.subtotal < Decimal("11000.00")

    def test_annual_cancellation_on_29_february_issues_a_credit(
        self, db_session, subscription, subscriber_account, monkeypatch
    ):
        from app.services import billing_automation

        _bill_one_quarter(
            db_session,
            subscription,
            subscriber_account,
            cycle=BillingCycle.annual,
            next_billing_at=datetime(2024, 2, 29, tzinfo=UTC),
        )
        _freeze_billing_clock(monkeypatch, datetime(2024, 2, 20, tzinfo=UTC))

        billing_automation.generate_cancellation_credit(db_session, subscription)

        credit = _issued_credit(db_session, subscriber_account)
        assert credit is not None, "29 February cancellation issued NO credit"
        # 9 unused days of the 366-day year 2023-02-28 .. 2024-02-29.
        assert credit.subtotal == Decimal("737.70")

    def test_reinjecting_the_deleted_reverse_reproduces_both_defects(
        self, db_session, subscription, subscriber_account, monkeypatch
    ):
        """Sensitivity proof for the two tests above, through the real path."""
        from app.services import billing_automation

        owner = _owner_module()
        monkeypatch.setattr(owner, "billing_cycle_start", _legacy_billing_cycle_start)

        _bill_one_quarter(
            db_session,
            subscription,
            subscriber_account,
            cycle=BillingCycle.quarterly,
            next_billing_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        _freeze_billing_clock(monkeypatch, datetime(2026, 3, 2, tzinfo=UTC))

        billing_automation.generate_cancellation_credit(db_session, subscription)
        credit = _issued_credit(db_session, subscriber_account)
        assert credit is not None
        assert credit.subtotal == Decimal("29032.26"), "DEFECT 1 no longer reproduces"

        # And the leap-day crash the caller swallows.
        subscription.billing_cycle = BillingCycle.annual
        subscription.next_billing_at = datetime(2024, 2, 29, tzinfo=UTC)
        db_session.flush()
        _freeze_billing_clock(monkeypatch, datetime(2024, 2, 20, tzinfo=UTC))
        with pytest.raises(ValueError, match="day is out of range for month"):
            billing_automation.generate_cancellation_credit(db_session, subscription)

    def test_a_cancellation_credit_failure_is_logged_with_its_traceback(
        self, db_session, subscription, subscriber_account, monkeypatch, caplog
    ):
        """DEFECT 2's silencer: the swallow must at least carry the frame."""
        import logging

        from app.services import account_lifecycle, billing_automation

        _bill_one_quarter(
            db_session,
            subscription,
            subscriber_account,
            cycle=BillingCycle.quarterly,
            next_billing_at=datetime(2026, 4, 1, tzinfo=UTC),
        )

        def _boom(db, sub):
            raise ValueError("day is out of range for month")

        monkeypatch.setattr(billing_automation, "generate_cancellation_credit", _boom)
        with caplog.at_level(logging.ERROR, logger=account_lifecycle.__name__):
            account_lifecycle.cancel_subscription(
                db_session,
                str(subscription.id),
                cancel_reason="test",
                source="pytest",
                emit=False,
            )
        failures = [
            r for r in caplog.records if "Cancellation credit" in r.getMessage()
        ]
        assert failures, "a swallowed credit failure produced no ERROR record"
        assert failures[0].levelno >= logging.ERROR
        assert failures[0].exc_info is not None, "no traceback captured"
