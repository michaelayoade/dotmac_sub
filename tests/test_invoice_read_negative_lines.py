"""Regression: invoice read schemas must serialize negative (credit) money.

A real invoice can carry a negative line (a credit/adjustment/true-up). The
read/response models used to inherit the create-side ``ge=0`` constraint, so
``/api/v1/invoices`` 500'd with ResponseValidationError on any such invoice.
The read models now reflect stored data without the constraint.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.billing import InvoiceLineRead, InvoiceRead


def test_invoice_line_read_allows_negative_amounts():
    line = InvoiceLineRead.model_validate(
        {
            "id": uuid.uuid4(),
            "invoice_id": uuid.uuid4(),
            "description": "Opening-balance true-up (credit)",
            "quantity": Decimal("1.000"),
            "unit_price": Decimal("-16333.00"),
            "amount": Decimal("-16333.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    assert line.unit_price == Decimal("-16333.00")
    assert line.amount == Decimal("-16333.00")


def test_invoice_read_allows_negative_totals_and_lines():
    inv = InvoiceRead.model_validate(
        {
            "id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "subtotal": Decimal("-16333.00"),
            "tax_total": Decimal("0.00"),
            "total": Decimal("-16333.00"),
            "balance_due": Decimal("-16333.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "lines": [
                {
                    "id": uuid.uuid4(),
                    "invoice_id": uuid.uuid4(),
                    "description": "Credit",
                    "quantity": Decimal("1.000"),
                    "unit_price": Decimal("-16333.00"),
                    "amount": Decimal("-16333.00"),
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            ],
        }
    )
    assert inv.total == Decimal("-16333.00")
    assert inv.lines[0].amount == Decimal("-16333.00")
