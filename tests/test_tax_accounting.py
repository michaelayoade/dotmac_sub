from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Payment,
)
from app.models.payment_proof import (
    WithholdingTaxRecord,
    WithholdingTaxStatus,
    WithholdingTaxTransition,
    WithholdingTaxTransitionImmutableError,
)
from app.models.subscriber import Reseller
from app.services import billing as billing_service
from app.services import tax_accounting


def _invoice(
    subscriber,
    *,
    number: str,
    issued_at: datetime,
    status: InvoiceStatus = InvoiceStatus.issued,
    currency: str = "NGN",
    tax: str = "7.50",
    total: str = "107.50",
    is_proforma: bool = False,
) -> Invoice:
    return Invoice(
        account_id=subscriber.id,
        invoice_number=number,
        status=status,
        currency=currency,
        subtotal=Decimal(total) - Decimal(tax),
        tax_total=Decimal(tax),
        total=Decimal(total),
        balance_due=Decimal(total),
        issued_at=issued_at,
        created_at=issued_at,
        is_proforma=is_proforma,
        is_active=True,
    )


def _wht_record(db_session, *, payment=None, status=WithholdingTaxStatus.pending):
    reseller = Reseller(
        name=f"Tax Reseller {status.value}",
        contact_email=f"tax-{status.value}@example.com",
    )
    db_session.add(reseller)
    db_session.commit()
    account = billing_service.billing_accounts.get_for_reseller(
        db_session, str(reseller.id)
    )
    record = WithholdingTaxRecord(
        billing_account_id=account.id,
        reseller_id=reseller.id,
        payment_id=payment.id if payment is not None else None,
        gross_amount=Decimal("100000.00"),
        net_amount=Decimal("95000.00"),
        wht_amount=Decimal("5000.00"),
        wht_rate=Decimal("5.00"),
        currency="NGN",
        status=status,
        created_at=datetime(2026, 2, 10, 9, tzinfo=UTC),
    )
    db_session.add(record)
    db_session.commit()
    return record


def test_tax_report_filters_tax_points_and_separates_currencies(db_session, subscriber):
    inside = datetime(2026, 1, 15, 12, tzinfo=UTC)
    db_session.add_all(
        [
            _invoice(subscriber, number="NG-1", issued_at=inside),
            _invoice(
                subscriber,
                number="USD-1",
                issued_at=inside,
                currency="usd",
                tax="10.00",
                total="110.00",
            ),
            _invoice(
                subscriber,
                number="OUTSIDE",
                issued_at=datetime(2025, 12, 31, 23, tzinfo=UTC),
            ),
            _invoice(
                subscriber,
                number="DRAFT",
                issued_at=inside,
                status=InvoiceStatus.draft,
            ),
            _invoice(
                subscriber,
                number="VOID",
                issued_at=inside,
                status=InvoiceStatus.void,
            ),
            _invoice(
                subscriber,
                number="PROFORMA",
                issued_at=inside,
                is_proforma=True,
            ),
        ]
    )
    db_session.commit()

    data = tax_accounting.build_tax_report(
        db_session, date_from="2026-01-01", date_to="2026-01-31"
    )

    assert {row["invoice_number"] for row in data["invoice_rows"]} == {
        "NG-1",
        "USD-1",
    }
    assert data["output_tax_totals"] == [
        {
            "currency": "NGN",
            "invoice_count": 1,
            "tax_amount": Decimal("7.50"),
            "gross_amount": Decimal("107.50"),
        },
        {
            "currency": "USD",
            "invoice_count": 1,
            "tax_amount": Decimal("10.00"),
            "gross_amount": Decimal("110.00"),
        },
    ]
    assert data["output_tax_invoice_count"] == 2
    assert "total_tax" not in data


def test_tax_report_subtracts_credit_note_at_persisted_issuance_point(
    db_session, subscriber
):
    db_session.add(
        _invoice(
            subscriber,
            number="NET-TAX-INVOICE",
            issued_at=datetime(2026, 2, 5, tzinfo=UTC),
            tax="75.00",
            total="1075.00",
        )
    )
    db_session.add(
        CreditNote(
            account_id=subscriber.id,
            credit_number="NET-TAX-CREDIT",
            status=CreditNoteStatus.issued,
            currency="NGN",
            subtotal=Decimal("200.00"),
            tax_total=Decimal("15.00"),
            total=Decimal("215.00"),
            is_active=True,
            created_at=datetime(2026, 1, 20, tzinfo=UTC),
            issued_at=datetime(2026, 2, 5, tzinfo=UTC),
        )
    )
    db_session.commit()

    january = tax_accounting.build_tax_report(
        db_session, date_from="2026-01-01", date_to="2026-01-31"
    )
    february = tax_accounting.build_tax_report(
        db_session, date_from="2026-02-01", date_to="2026-02-28"
    )

    assert january["credit_note_count"] == 0
    assert february["credit_note_count"] == 1
    assert february["net_output_tax_totals"][0]["net_output_tax_liability"] == (
        Decimal("60.00")
    )


def test_tax_report_keeps_wht_receivable_separate_from_net_cash(db_session):
    pending = _wht_record(db_session)
    reclaimed = _wht_record(db_session, status=WithholdingTaxStatus.reclaimed)
    data = tax_accounting.build_tax_report(
        db_session, date_from="2026-02-01", date_to="2026-02-28"
    )

    assert {row["record_id"] for row in data["wht_rows"]} == {
        str(pending.id),
        str(reclaimed.id),
    }
    assert data["wht_totals"] == [
        {
            "currency": "NGN",
            "record_count": 2,
            "gross_amount": Decimal("200000.00"),
            "net_cash_amount": Decimal("190000.00"),
            "wht_amount": Decimal("10000.00"),
            "outstanding_wht_amount": Decimal("5000.00"),
            "by_status": {
                "pending": Decimal("5000.00"),
                "reclaimed": Decimal("5000.00"),
            },
        }
    ]


def test_tax_report_projection_is_read_only(db_session, subscriber):
    db_session.add(
        _invoice(
            subscriber,
            number="READ-ONLY",
            issued_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    db_session.commit()

    tax_accounting.build_tax_report(db_session)

    assert db_session.query(LedgerEntry).count() == 0
    assert not db_session.dirty
    assert not db_session.new


def test_wht_operator_queue_filters_searches_and_paginates(db_session):
    reseller = Reseller(
        name="Paged Tax Reseller", contact_email="paged-tax@example.com"
    )
    db_session.add(reseller)
    db_session.commit()
    account = billing_service.billing_accounts.get_for_reseller(
        db_session, str(reseller.id)
    )
    db_session.add_all(
        [
            WithholdingTaxRecord(
                billing_account_id=account.id,
                reseller_id=reseller.id,
                gross_amount=Decimal("1000.00"),
                net_amount=Decimal("950.00"),
                wht_amount=Decimal("50.00"),
                currency="NGN",
                status=(
                    WithholdingTaxStatus.certified
                    if index == 25
                    else WithholdingTaxStatus.pending
                ),
                certificate_reference=f"WHT-PAGE-{index:03d}",
            )
            for index in range(26)
        ]
    )
    db_session.commit()

    first = tax_accounting.build_tax_operations_state(db_session)
    second = tax_accounting.build_tax_operations_state(db_session, wht_page=2)
    filtered = tax_accounting.build_tax_operations_state(
        db_session,
        wht_status=WithholdingTaxStatus.certified,
        wht_search="WHT-PAGE-025",
    )

    assert first["accounting_owner"] == "dotmac_erp"
    assert len(first["wht_records"]) == 25
    assert first["wht_pagination"] == {
        "page": 1,
        "page_size": 25,
        "total": 26,
        "page_count": 2,
        "has_previous": False,
        "has_next": True,
    }
    assert len(second["wht_records"]) == 1
    assert filtered["wht_records"][0].certificate_reference == "WHT-PAGE-025"


def test_wht_lifecycle_requires_evidence_records_timeline_and_advances_payment(
    db_session, subscriber_account
):
    payment = Payment(
        account_id=subscriber_account.id,
        amount=Decimal("100000.00"),
        currency="NGN",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(payment)
    db_session.commit()
    record = _wht_record(db_session, payment=payment)

    with pytest.raises(tax_accounting.TaxAccountingError, match="certificate"):
        tax_accounting.transition_withholding_tax(
            db_session,
            record.id,
            target_status=WithholdingTaxStatus.certified,
            actor_id="finance-1",
        )

    certified = tax_accounting.transition_withholding_tax(
        db_session,
        record.id,
        target_status=WithholdingTaxStatus.certified,
        actor_id="finance-1",
        certificate_reference="WHT-CERT-2026-001",
    )
    reclaimed = tax_accounting.transition_withholding_tax(
        db_session,
        record.id,
        target_status=WithholdingTaxStatus.reclaimed,
        actor_id="finance-2",
        notes="Reclaimed in June filing",
    )

    db_session.refresh(payment)
    assert certified.certified_at is not None
    assert reclaimed.resolved_at is not None
    payment_updated_at = payment.updated_at
    if payment_updated_at.tzinfo is None:
        payment_updated_at = payment_updated_at.replace(tzinfo=UTC)
    assert payment_updated_at > datetime(2026, 1, 1, tzinfo=UTC)
    transitions = list(
        db_session.scalars(
            select(WithholdingTaxTransition)
            .where(WithholdingTaxTransition.record_id == record.id)
            .order_by(WithholdingTaxTransition.occurred_at)
        ).all()
    )
    assert [(item.from_status, item.to_status) for item in transitions] == [
        (None, WithholdingTaxStatus.pending),
        (WithholdingTaxStatus.pending, WithholdingTaxStatus.certified),
        (WithholdingTaxStatus.certified, WithholdingTaxStatus.reclaimed),
    ]
    transitions[0].notes = "rewrite history"
    with pytest.raises(WithholdingTaxTransitionImmutableError):
        db_session.commit()
    db_session.rollback()


def test_wht_lifecycle_rejects_illegal_or_unexplained_terminal_transitions(
    db_session,
):
    record = _wht_record(db_session)
    with pytest.raises(tax_accounting.TaxAccountingError, match="write-off reason"):
        tax_accounting.transition_withholding_tax(
            db_session,
            record.id,
            target_status=WithholdingTaxStatus.written_off,
            actor_id="finance-1",
        )
    with pytest.raises(tax_accounting.TaxAccountingError, match="Illegal WHT"):
        tax_accounting.transition_withholding_tax(
            db_session,
            record.id,
            target_status=WithholdingTaxStatus.reclaimed,
            actor_id="finance-1",
        )
