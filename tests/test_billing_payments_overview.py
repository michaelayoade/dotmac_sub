from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentMethod,
    PaymentMethodType,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.web_billing_payments import build_payments_list_data, render_payments_csv


def _create_payment_method(db_session, account_id, method_type: PaymentMethodType) -> PaymentMethod:
    method = PaymentMethod(
        account_id=account_id,
        method_type=method_type,
        label=method_type.value,
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()
    db_session.refresh(method)
    return method


def _create_payment(
    db_session,
    *,
    account_id,
    amount: str,
    status: PaymentStatus,
    created_at: datetime,
    memo: str,
    payment_method_id=None,
):
    payment = Payment(
        account_id=account_id,
        amount=Decimal(amount),
        currency="NGN",
        status=status,
        memo=memo,
        payment_method_id=payment_method_id,
        created_at=created_at,
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    return payment


def test_build_payments_list_data_filters_by_status_and_method(db_session, subscriber):
    card = _create_payment_method(db_session, subscriber.id, PaymentMethodType.card)
    cash = _create_payment_method(db_session, subscriber.id, PaymentMethodType.cash)
    now = datetime.now(UTC)

    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="100",
        status=PaymentStatus.succeeded,
        created_at=now,
        memo="card payment",
        payment_method_id=card.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="50",
        status=PaymentStatus.pending,
        created_at=now,
        memo="cash pending",
        payment_method_id=cash.id,
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        status="succeeded",
        method="card",
        search=None,
        date_range=None,
    )

    assert result["total"] == 1
    assert len(result["payments"]) == 1
    assert result["payments"][0].display_method == "Card"


def test_build_payments_list_data_search_and_date_range(db_session, subscriber):
    method = _create_payment_method(db_session, subscriber.id, PaymentMethodType.transfer)
    now = datetime.now(UTC)
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="120",
        status=PaymentStatus.succeeded,
        created_at=now - timedelta(days=2),
        memo="NIP/WEMA/BANK/ENE CONNECTIVITY",
        payment_method_id=method.id,
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="80",
        status=PaymentStatus.succeeded,
        created_at=now - timedelta(days=50),
        memo="old transfer",
        payment_method_id=method.id,
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        status=None,
        method=None,
        search="WEMA",
        date_range="month",
    )

    assert result["total"] == 1
    payment = result["payments"][0]
    assert "Bank" in payment.display_number
    assert "WEMA" in payment.narration


def test_render_payments_csv_contains_narration_and_method(db_session, subscriber):
    method = _create_payment_method(db_session, subscriber.id, PaymentMethodType.cash)
    payment = _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="30",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="cash till",
        payment_method_id=method.id,
    )
    payment.display_number = "Cash 123"  # type: ignore[attr-defined]
    payment.display_method = "Cash"  # type: ignore[attr-defined]
    payment.narration = "cash till"  # type: ignore[attr-defined]

    csv_text = render_payments_csv([payment])

    assert "display_number" in csv_text
    assert "Cash 123" in csv_text
    assert "cash till" in csv_text


def test_build_payments_list_data_unallocated_only(db_session, subscriber):
    method = _create_payment_method(db_session, subscriber.id, PaymentMethodType.transfer)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    allocated = _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="100",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="allocated",
        payment_method_id=method.id,
    )
    db_session.add(
        PaymentAllocation(
            payment_id=allocated.id,
            invoice_id=invoice.id,
            amount=Decimal("100.00"),
        )
    )
    db_session.commit()

    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="55",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="unallocated",
        payment_method_id=method.id,
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        status=None,
        method=None,
        search=None,
        date_range=None,
        unallocated_only=True,
    )

    assert result["total"] == 1
    assert result["payments"][0].memo == "unallocated"


def test_build_payments_list_data_filters_by_partner(db_session):
    reseller_a = Reseller(name="Partner A")
    reseller_b = Reseller(name="Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Pay",
        last_name="A",
        email="pay-a@example.com",
        reseller_id=reseller_a.id,
    )
    account_b = Subscriber(
        first_name="Pay",
        last_name="B",
        email="pay-b@example.com",
        reseller_id=reseller_b.id,
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    _create_payment(
        db_session,
        account_id=account_a.id,
        amount="100",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="partner a payment",
    )
    _create_payment(
        db_session,
        account_id=account_b.id,
        amount="70",
        status=PaymentStatus.succeeded,
        created_at=datetime.now(UTC),
        memo="partner b payment",
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=25,
        customer_ref=None,
        partner_id=str(reseller_a.id),
        status=None,
        method=None,
        search=None,
        date_range=None,
    )

    assert result["total"] == 1
    assert len(result["payments"]) == 1
    assert result["payments"][0].memo == "partner a payment"
    assert result["selected_partner_id"] == str(reseller_a.id)


def test_build_payments_list_data_includes_status_totals_for_filtered_set(db_session, subscriber):
    now = datetime.now(UTC)
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="10",
        status=PaymentStatus.succeeded,
        created_at=now,
        memo="ok",
    )
    _create_payment(
        db_session,
        account_id=subscriber.id,
        amount="20",
        status=PaymentStatus.pending,
        created_at=now,
        memo="wait",
    )

    result = build_payments_list_data(
        db_session,
        page=1,
        per_page=1,
        customer_ref=None,
        partner_id=None,
        status=None,
        method=None,
        search=None,
        date_range=None,
    )

    assert result["total"] == 2
    assert result["status_totals"]["succeeded"]["count"] == 1
    assert result["status_totals"]["pending"]["count"] == 1
    assert result["status_totals"]["all"]["count"] == 2
    assert result["status_totals"]["all"]["amount"] == 30.0
