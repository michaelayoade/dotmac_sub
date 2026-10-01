from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.billing import TaxApplication
from app.models.catalog import BillingCycle
from app.services.billing.cadence import service_period
from app.services.prepaid_period_purchases import (
    _line_fingerprint,
    _monthly_cadence,
    _quote_fingerprint,
)
from app.services.prepaid_service_renewals import PrepaidMonthlyChargeDetail


def _charge(
    *, tax: str = "562.50", total: str = "8062.50"
) -> PrepaidMonthlyChargeDetail:
    return PrepaidMonthlyChargeDetail(
        subscription_id=uuid4(),
        unit_price=Decimal("7500.00"),
        subtotal=Decimal("7500.00"),
        tax_total=Decimal(tax),
        total=Decimal(total),
        currency="NGN",
        billing_cycle=BillingCycle.monthly,
        tax_rate_id=uuid4(),
        tax_application=TaxApplication.exclusive,
    )


def test_monthly_periods_remain_contiguous_across_short_months() -> None:
    anchor = datetime(2026, 1, 31, tzinfo=UTC)
    periods = [
        service_period(cadence=_monthly_cadence(), contract_start=anchor, index=index)
        for index in range(3)
    ]
    assert periods[0].ends_at == periods[1].starts_at
    assert periods[1].ends_at == periods[2].starts_at
    assert periods[2].ends_at.day == 30


def test_per_period_vat_is_part_of_each_fingerprint() -> None:
    starts_at = datetime(2026, 1, 1, tzinfo=UTC)
    ends_at = datetime(2026, 2, 1, tzinfo=UTC)
    normal = _line_fingerprint(
        ordinal=1, starts_at=starts_at, ends_at=ends_at, charge=_charge()
    )
    changed = _line_fingerprint(
        ordinal=1,
        starts_at=starts_at,
        ends_at=ends_at,
        charge=_charge(tax="562.49", total="8062.49"),
    )
    assert normal != changed


def test_quote_fingerprint_is_order_sensitive() -> None:
    first = _quote_fingerprint({"periods": ["a", "b"], "total": "10.00"})
    second = _quote_fingerprint({"periods": ["b", "a"], "total": "10.00"})
    assert first != second
