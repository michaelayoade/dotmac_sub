"""Money-correctness tests for the admin pricing calculator helpers.

These lock the taxable-base and proration rules that the calculator's client-side
JS mirrors, matching how real invoicing computes VAT and prorated charges.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.models.catalog import BillingCycle
from app.services import web_catalog_calculator as calc


def test_monthly_full_period_no_one_time_unchanged():
    """Byte-identical baseline: recurring-only monthly with exclusive VAT."""
    result = calc.compute_monthly(
        recurring_subtotal=Decimal("10000"),
        overage_charge=Decimal("0"),
        with_vat=True,
        vat_percent=Decimal("7.5"),
    )
    assert result["subtotal"] == Decimal("10000.00")
    assert result["vat_amount"] == Decimal("750.00")
    assert result["total"] == Decimal("10750.00")


def test_first_bill_full_period_equals_monthly_when_no_one_time():
    """Full period + no one-time fee => first bill == monthly total (preserved)."""
    monthly = calc.compute_monthly(
        recurring_subtotal=Decimal("10000"),
        overage_charge=Decimal("0"),
        with_vat=True,
        vat_percent=Decimal("7.5"),
    )
    first = calc.compute_first_bill(
        recurring_subtotal=Decimal("10000"),
        one_time_total=Decimal("0"),
        overage_charge=Decimal("0"),
        with_vat=True,
        vat_percent=Decimal("7.5"),
    )
    assert first["proration_ratio"] == Decimal("1")
    assert first["total"] == monthly["total"] == Decimal("10750.00")


def test_first_bill_vat_includes_taxable_one_time_fee():
    """VAT must apply to one-time fees too — the flagged bug.

    Recurring 10000 + one-time 5000 => taxable base 15000; VAT@7.5% = 1125.
    (Old behaviour taxed only the 10000 recurring and added the 5000 VAT-free.)
    """
    first = calc.compute_first_bill(
        recurring_subtotal=Decimal("10000"),
        one_time_total=Decimal("5000"),
        overage_charge=Decimal("0"),
        with_vat=True,
        vat_percent=Decimal("7.5"),
    )
    assert first["taxable_base"] == Decimal("15000.00")
    assert first["vat_amount"] == Decimal("1125.00")
    assert first["total"] == Decimal("16125.00")
    # The buggy VAT-free-one-time total would have been 10750 + 5000 = 15750.
    assert first["total"] != Decimal("15750.00")


def test_first_bill_no_vat_leaves_one_time_untaxed():
    first = calc.compute_first_bill(
        recurring_subtotal=Decimal("10000"),
        one_time_total=Decimal("5000"),
        overage_charge=Decimal("0"),
        with_vat=False,
        vat_percent=Decimal("0"),
    )
    assert first["vat_amount"] == Decimal("0.00")
    assert first["total"] == Decimal("15000.00")


def test_proration_ratio_mid_month():
    """Jan 16 start of a 31-day month leaves 16 days (Jan 16 -> Feb 1)."""
    ratio = calc.first_bill_proration_ratio(
        datetime(2026, 1, 16, tzinfo=UTC), BillingCycle.monthly
    )
    assert ratio == Decimal("16") / Decimal("31")


def test_proration_ratio_full_on_cycle_boundary():
    """A start on the first of the month consumes the whole cycle (ratio 1)."""
    ratio = calc.first_bill_proration_ratio(
        datetime(2026, 1, 1, tzinfo=UTC), BillingCycle.monthly
    )
    assert ratio == Decimal("1")


def test_first_bill_prorates_recurring_but_not_one_time():
    """Mid-cycle first bill: recurring prorated, one-time charged in full."""
    first = calc.compute_first_bill(
        recurring_subtotal=Decimal("31000"),
        one_time_total=Decimal("5000"),
        overage_charge=Decimal("0"),
        with_vat=True,
        vat_percent=Decimal("7.5"),
        start=datetime(2026, 1, 16, tzinfo=UTC),
        billing_cycle="monthly",
    )
    # 31000 * 16/31 = 16000 recurring; + 5000 one-time (full) = 21000 base.
    assert first["prorated_recurring"] == Decimal("16000.00")
    assert first["taxable_base"] == Decimal("21000.00")
    assert first["vat_amount"] == Decimal("1575.00")
    assert first["total"] == Decimal("22575.00")
