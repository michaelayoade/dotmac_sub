from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import Payment, PaymentMethod, PaymentMethodType, PaymentStatus
from app.services import web_reports


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


def test_revenue_export_uses_payment_method_label(db_session, subscriber):
    method = PaymentMethod(
        account_id=subscriber.id,
        method_type=PaymentMethodType.transfer,
        label="Bank transfer",
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()

    paid_at = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    _payment(
        db_session,
        account_id=subscriber.id,
        paid_at=paid_at,
        payment_method_id=method.id,
        amount="1250.00",
    )

    rows = list(
        csv.DictReader(io.StringIO(web_reports.build_revenue_export_csv(db_session)))
    )

    assert len(rows) == 1
    assert rows[0]["account_id"] == str(subscriber.id)
    assert rows[0]["amount"] == "1250.00"
    assert rows[0]["status"] == PaymentStatus.succeeded.value
    assert rows[0]["payment_method"] == "Bank transfer"
