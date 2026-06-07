"""Payment-arrangement eligibility must be computed and returned.

Regression: get_new_arrangement_page never set `eligible`, so the template's
`{% if not eligible %}` always fired — every customer saw "Not Eligible" and the
form never rendered (0 arrangements ever created). It also omitted
`overdue_invoices`/`oldest_due_date` that the template reads.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.services.customer_portal_flow_billing import get_new_arrangement_page


def _overdue_invoice(db_session, subscriber, amount="10000.00"):
    inv = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal(amount),
        tax_total=Decimal("0.00"),
        total=Decimal(amount),
        balance_due=Decimal(amount),
        issued_at=datetime(2026, 4, 1, tzinfo=UTC),
        due_at=datetime(2026, 5, 1, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(inv)
    db_session.commit()
    return inv


def test_eligible_with_overdue_invoice(db_session, subscriber):
    _overdue_invoice(db_session, subscriber)
    page = get_new_arrangement_page(db_session, {"account_id": str(subscriber.id)})

    assert page["eligible"] is True
    assert page["ineligible_reason"] is None
    assert page["outstanding_balance"] > 0
    assert len(page["overdue_invoices"]) == 1
    assert page["oldest_due_date"] is not None


def test_not_eligible_without_overdue_balance(db_session, subscriber):
    page = get_new_arrangement_page(db_session, {"account_id": str(subscriber.id)})

    assert page["eligible"] is False
    assert page["ineligible_reason"]  # a reason is provided
    assert page["overdue_invoices"] == []
