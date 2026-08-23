"""Money-impact candidates preserve issued evidence and fail closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
    TaxRate,
)
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.services.billing_tax_reconciliation import (
    TaxReconciliationConfidence,
    TaxReconciliationReason,
    get_tax_reconciliation_candidate,
    list_tax_reconciliation_candidates,
)
from app.services.domain_errors import DomainError
from app.services.web_billing_tax_reconciliation import (
    issue_tax_credit,
    prepare_tax_credit_review,
)


def _taxed_invoice(
    db_session,
    *,
    subscriber,
    subscription,
    tax_rate,
    issued_at: datetime,
    status: InvoiceStatus = InvoiceStatus.issued,
    tax_total: Decimal = Decimal("750.00"),
) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-TAX-{issued_at.timestamp()}",
        status=status,
        currency="NGN",
        subtotal=Decimal("10000.00"),
        tax_total=tax_total,
        total=Decimal("10000.00") + tax_total,
        balance_due=Decimal("10000.00") + tax_total,
        issued_at=issued_at,
        is_proforma=False,
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Subscription service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("10000.00"),
            amount=Decimal("10000.00"),
            tax_rate_id=tax_rate.id,
            tax_application=TaxApplication.exclusive,
            is_active=True,
        )
    )
    db_session.commit()
    return invoice


@pytest.fixture()
def vat_rate(db_session) -> TaxRate:
    rate = TaxRate(
        name="VAT reconciliation",
        code="VAT-RECONCILIATION",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(rate)
    db_session.commit()
    return rate


def _exempt_policy(db_session, *, subscriber, effective_at: datetime):
    policy = CustomerTaxPolicy(
        account_id=subscriber.id,
        withholding_tax_enabled=False,
        vat_exempt=True,
        version=3,
        updated_by="pytest",
        created_at=effective_at,
        updated_at=effective_at,
    )
    db_session.add(policy)
    db_session.commit()
    return policy


def test_post_policy_subscription_invoice_is_confirmed_and_credit_is_subtracted(
    db_session, subscriber, subscription, vat_rate
):
    now = datetime.now(UTC)
    policy = _exempt_policy(
        db_session,
        subscriber=subscriber,
        effective_at=now - timedelta(days=2),
    )
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=now - timedelta(days=1),
    )
    db_session.add(
        CreditNote(
            account_id=subscriber.id,
            invoice_id=invoice.id,
            status=CreditNoteStatus.issued,
            currency="NGN",
            subtotal=Decimal("0.00"),
            tax_total=Decimal("250.00"),
            total=Decimal("250.00"),
            applied_total=Decimal("0.00"),
            issued_at=now,
            is_active=True,
        )
    )
    db_session.commit()

    candidate = get_tax_reconciliation_candidate(db_session, invoice.id)

    assert candidate is not None
    assert candidate.reason == TaxReconciliationReason.confirmed_customer_exemption
    assert candidate.confidence == TaxReconciliationConfidence.confirmed
    assert candidate.customer_tax_policy_id == policy.id
    assert candidate.customer_tax_policy_version == 3
    assert candidate.source_tax_rate_id == vat_rate.id
    assert candidate.observed_tax_total == Decimal("750.00")
    assert candidate.credited_tax_total == Decimal("250.00")
    assert candidate.maximum_remaining_adjustment == Decimal("500.00")
    assert candidate.can_prepare_tax_credit is True


def test_pre_policy_invoice_is_review_only_because_policy_history_is_missing(
    db_session, subscriber, subscription, vat_rate
):
    now = datetime.now(UTC)
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=now - timedelta(days=3),
    )
    _exempt_policy(
        db_session,
        subscriber=subscriber,
        effective_at=now - timedelta(days=1),
    )

    candidate = get_tax_reconciliation_candidate(db_session, invoice.id)

    assert candidate is not None
    assert candidate.reason == TaxReconciliationReason.exemption_timing_unproven
    assert candidate.can_prepare_tax_credit is False


def test_mixed_tax_scope_is_review_only(db_session, subscriber, subscription, vat_rate):
    now = datetime.now(UTC)
    _exempt_policy(
        db_session,
        subscriber=subscriber,
        effective_at=now - timedelta(days=2),
    )
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=now - timedelta(days=1),
    )
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=None,
            description="Taxed installation",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.00"),
            amount=Decimal("1000.00"),
            tax_rate_id=vat_rate.id,
            tax_application=TaxApplication.exclusive,
            is_active=True,
        )
    )
    db_session.commit()

    candidate = get_tax_reconciliation_candidate(db_session, invoice.id)

    assert candidate is not None
    assert candidate.reason == TaxReconciliationReason.mixed_invoice_tax_scope
    assert candidate.can_prepare_tax_credit is False


def test_legacy_inclusive_label_is_an_ambiguity_not_a_refund_decision(
    db_session, subscriber, subscription, vat_rate
):
    subscription.offer.with_vat = True
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=datetime.now(UTC),
    )

    candidate = get_tax_reconciliation_candidate(db_session, invoice.id)

    assert candidate is not None
    assert candidate.reason == TaxReconciliationReason.inclusive_label_ambiguity
    assert candidate.maximum_remaining_adjustment == Decimal("750.00")
    assert candidate.can_prepare_tax_credit is False


@pytest.mark.parametrize(
    ("status", "tax_total"),
    (
        (InvoiceStatus.draft, Decimal("750.00")),
        (InvoiceStatus.void, Decimal("750.00")),
        (InvoiceStatus.issued, Decimal("0.00")),
    ),
)
def test_non_issued_or_untaxed_documents_are_not_candidates(
    db_session,
    subscriber,
    subscription,
    vat_rate,
    status,
    tax_total,
):
    now = datetime.now(UTC)
    _exempt_policy(
        db_session,
        subscriber=subscriber,
        effective_at=now - timedelta(days=1),
    )
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=now,
        status=status,
        tax_total=tax_total,
    )

    assert get_tax_reconciliation_candidate(db_session, invoice.id) is None


def test_confirmed_credit_uses_credit_note_owner_and_preserves_invoice(
    db_session, subscriber, subscription, vat_rate
):
    now = datetime.now(UTC)
    _exempt_policy(
        db_session,
        subscriber=subscriber,
        effective_at=now - timedelta(days=2),
    )
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=now - timedelta(days=1),
    )
    original_total = invoice.total
    original_balance_due = invoice.balance_due
    candidate = get_tax_reconciliation_candidate(db_session, invoice.id)
    assert candidate is not None
    review = prepare_tax_credit_review(
        db_session,
        invoice_id=invoice.id,
        candidate_fingerprint=candidate.fingerprint,
    )

    result = issue_tax_credit(
        db_session,
        invoice_id=invoice.id,
        candidate_fingerprint=candidate.fingerprint,
        preview_fingerprint=review.preview.fingerprint,
        idempotency_key=review.idempotency_key,
    )

    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.partially_paid
    assert invoice.total == original_total
    assert invoice.balance_due == original_balance_due - Decimal("750.00")
    assert result.credit_note.invoice_id == invoice.id
    assert result.credit_note.subtotal == Decimal("0.00")
    assert result.credit_note.tax_total == Decimal("750.00")
    assert result.credit_note.total == Decimal("750.00")
    assert result.credit_note.funding_ledger_entry_id is not None
    assert get_tax_reconciliation_candidate(db_session, invoice.id) is None


def test_ambiguous_candidate_cannot_reach_credit_issuance(
    db_session, subscriber, subscription, vat_rate
):
    subscription.offer.with_vat = True
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=datetime.now(UTC),
    )
    candidate = get_tax_reconciliation_candidate(db_session, invoice.id)
    assert candidate is not None

    with pytest.raises(DomainError, match="does not prove an exact"):
        prepare_tax_credit_review(
            db_session,
            invoice_id=invoice.id,
            candidate_fingerprint=candidate.fingerprint,
        )

    page = list_tax_reconciliation_candidates(db_session, limit=10)
    assert tuple(item.invoice_id for item in page.candidates) == (invoice.id,)


def test_changed_policy_version_invalidates_the_operator_fingerprint(
    db_session, subscriber, subscription, vat_rate
):
    now = datetime.now(UTC)
    policy = _exempt_policy(
        db_session,
        subscriber=subscriber,
        effective_at=now - timedelta(days=2),
    )
    invoice = _taxed_invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        tax_rate=vat_rate,
        issued_at=now - timedelta(days=1),
    )
    candidate = get_tax_reconciliation_candidate(db_session, invoice.id)
    assert candidate is not None
    policy.version += 1
    db_session.commit()

    with pytest.raises(DomainError, match="changed after review"):
        prepare_tax_credit_review(
            db_session,
            invoice_id=invoice.id,
            candidate_fingerprint=candidate.fingerprint,
        )
