from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.billing import Payment, PaymentMethod, PaymentMethodType, PaymentStatus
from app.services import web_reports
from app.services.billing import reporting as billing_reporting


def test_revenue_export_serializes_typed_payment_method_projection(monkeypatch):
    account_id = uuid4()
    paid_at = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    row = billing_reporting.RevenuePaymentExportRow(
        paid_at=paid_at,
        account_name="Example Customer",
        account_id=account_id,
        invoice_id=None,
        amount=Decimal("1250.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        payment_method="Bank transfer",
        provider="",
    )
    monkeypatch.setattr(
        billing_reporting,
        "get_revenue_payment_export_rows",
        lambda _db, *, query: (row,),
    )

    rows = list(
        csv.DictReader(io.StringIO(web_reports.build_revenue_export_csv(object())))
    )

    assert rows == [
        {
            "paid_at": paid_at.isoformat(),
            "account": "Example Customer",
            "account_id": str(account_id),
            "invoice_id": "",
            "amount": "1250.00",
            "currency": "NGN",
            "status": "succeeded",
            "payment_method": "Bank transfer",
            "provider": "",
        }
    ]


def _payment(
    db_session,
    *,
    account_id,
    paid_at: datetime,
    payment_method_id,
    amount: str,
) -> Payment:
    payment = Payment(
        account_id=account_id,
        payment_method_id=payment_method_id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        created_at=paid_at,
        is_active=True,
    )
    db_session.add(payment)
    db_session.commit()
    return payment


def test_revenue_export_uses_payment_method_label_and_filters_period(
    db_session, subscriber
):
    method = PaymentMethod(
        account_id=subscriber.id,
        method_type=PaymentMethodType.transfer,
        label="Bank transfer",
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()

    now = datetime.now(UTC)
    recent = _payment(
        db_session,
        account_id=subscriber.id,
        paid_at=now - timedelta(days=2),
        payment_method_id=method.id,
        amount="1250.00",
    )
    _payment(
        db_session,
        account_id=subscriber.id,
        paid_at=now - timedelta(days=60),
        payment_method_id=method.id,
        amount="750.00",
    )

    rows = list(
        csv.DictReader(
            io.StringIO(web_reports.build_revenue_export_csv(db_session, days=30))
        )
    )

    assert len(rows) == 1
    assert rows[0]["account_id"] == str(subscriber.id)
    assert rows[0]["amount"] == "1250.00"
    assert rows[0]["status"] == PaymentStatus.succeeded.value
    assert rows[0]["payment_method"] == "Bank transfer"
    assert rows[0]["paid_at"] == recent.paid_at.isoformat()
