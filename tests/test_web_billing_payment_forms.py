"""Payment-form balance derivation.

Regression: a fully-paid invoice (balance_due == 0) was treated as falsy and
fell back to invoice.total, so the record-payment form showed the full total as
"balance due" and pre-filled it — inviting a duplicate payment / overpayment.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.services.web_billing_payment_forms import invoice_balance_info


def _invoice(balance_due, total, currency="NGN"):
    return SimpleNamespace(balance_due=balance_due, total=total, currency=currency)


def test_paid_invoice_shows_zero_balance_not_total():
    value, display = invoice_balance_info(
        _invoice(Decimal("0.00"), Decimal("15000.00"))
    )
    assert value == "0.00"
    assert display == "NGN 0.00"


def test_partial_balance_is_shown():
    value, _ = invoice_balance_info(_invoice(Decimal("5000.00"), Decimal("15000.00")))
    assert value == "5000.00"


def test_unset_balance_falls_back_to_total():
    # Legacy/never-computed balance_due (None) should still fall back to total.
    value, display = invoice_balance_info(_invoice(None, Decimal("15000.00")))
    assert value == "15000.00"
    assert "15,000.00" in display
