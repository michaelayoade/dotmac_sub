from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.schemas.billing import (
    InvoiceLineCreate,
    InvoiceLineUpdate,
    InvoiceUpdate,
    PaymentAllocationApply,
    PaymentCreate,
    PaymentRefundPreviewRequest,
    PaymentRefundRequest,
)
from app.services.backdated_service_invoicing import (
    CREATE_PERMISSION,
    MARKER_KEY,
    OWNER,
    RECONSTRUCTION_REF_PREFIX,
    BackdatedServiceInvoiceAction,
    BackdatedServiceInvoiceError,
    BackdatedServiceInvoiceQuery,
    CreateBackdatedServiceInvoiceCommand,
    execute_backdated_service_invoice,
    preview_backdated_service_invoice,
)
from app.services.billing.invoices import (
    ControlledInvoiceDraftSeal,
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
    verified_draft_issuance,
)
from app.services.billing.payments import (
    PaymentAllocationFinalizationMode,
    Payments,
    Refunds,
    resolve_payment_allocation_finalization,
)
from app.services.owner_commands import CommandContext
from tests.prepaid_funding_helpers import ensure_test_prepaid_contract


def _prepare_subscription(db_session, subscription) -> None:
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.start_at = datetime(2026, 1, 1, tzinfo=UTC)
    subscription.next_billing_at = datetime(2026, 10, 16, 23, tzinfo=UTC)
    ensure_test_prepaid_contract(db_session, subscription, Decimal("52500.00"))
    db_session.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id=f"backdated-{uuid4()}",
            status_type=AccountingStatus.interim,
            session_start=datetime(2026, 6, 17, 8, tzinfo=UTC),
            last_update_at=datetime(2026, 6, 17, 18, tzinfo=UTC),
            input_octets=100,
            output_octets=200,
        )
    )
    reference = Invoice(
        account_id=subscription.subscriber_id,
        invoice_number=f"INV-TERMS-{uuid4()}",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("52500.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("52500.00"),
        balance_due=Decimal("52500.00"),
        billing_period_start=datetime(2026, 7, 16, 23, tzinfo=UTC),
        billing_period_end=datetime(2026, 8, 16, 23, tzinfo=UTC),
        issued_at=datetime(2026, 7, 16, 23, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(reference)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=reference.id,
            subscription_id=subscription.id,
            description="Adjacent monthly service terms evidence",
            quantity=Decimal("1.000"),
            unit_price=Decimal("52500.00"),
            amount=Decimal("52500.00"),
            metadata_={"kind": "base_subscription"},
            is_active=True,
        )
    )
    db_session.commit()


def _query(subscriber, subscription, **overrides):
    values = {
        "account_id": subscriber.id,
        "subscription_id": subscription.id,
        "starts_on": date(2026, 6, 17),
        "ends_on": date(2026, 7, 17),
    }
    values.update(overrides)
    return BackdatedServiceInvoiceQuery(**values)


def _command(query, fingerprint, *, permission_granted=True, scope=CREATE_PERMISSION):
    return CreateBackdatedServiceInvoiceCommand(
        context=CommandContext.system(
            actor="user:pytest",
            scope=scope,
            reason="Reviewed historical consumed service",
            idempotency_key=str(uuid4()),
        ),
        query=query,
        expected_fingerprint=fingerprint,
        permission_granted=permission_granted,
    )


def test_create_builds_linked_draft_and_historical_payment_consequence(
    db_session, subscriber, subscription
):
    _prepare_subscription(db_session, subscription)
    query = _query(subscriber, subscription)

    preview = preview_backdated_service_invoice(db_session, query)

    assert preview.actionable is True
    assert preview.total == Decimal("52500.00")
    assert preview.usage_interval_count == 1
    assert preview.terms_source_invoice_ids
    db_session.rollback()

    result = execute_backdated_service_invoice(
        db_session, _command(query, preview.fingerprint)
    )
    invoice = db_session.get(Invoice, result.invoice_id)
    line = db_session.get(InvoiceLine, result.invoice_line_id)

    assert invoice is not None
    assert line is not None
    assert invoice.status is InvoiceStatus.draft
    assert invoice.billing_period_start == preview.period_start
    assert invoice.billing_period_end == preview.period_end
    assert line.subscription_id == subscription.id
    assert line.metadata_[MARKER_KEY] is True
    assert line.metadata_["payment_consequence"] == "historical_entitlement_only"
    assert (
        resolve_payment_allocation_finalization(db_session, invoice).mode
        is PaymentAllocationFinalizationMode.reviewed_document_correction
    )

    original_anchor = subscription.next_billing_at
    Invoices.issue_draft_system(
        db_session,
        str(invoice.id),
        issuance=verified_draft_issuance(
            invoice,
            issued_at=datetime(2026, 10, 2, tzinfo=UTC),
            reason="pytest reviewed backdated issue",
        ),
        apply_available_credit=False,
        commit=False,
    )
    payment = Payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=invoice.total,
            currency=invoice.currency,
            status=PaymentStatus.succeeded,
            paid_at=datetime(2026, 10, 2, tzinfo=UTC),
            allocations=[
                PaymentAllocationApply(invoice_id=invoice.id, amount=invoice.total)
            ],
        ),
        auto_allocate=False,
        commit=False,
    )
    db_session.flush()
    assert payment.status is PaymentStatus.succeeded
    entitlement = (
        db_session.query(ServiceEntitlement)
        .filter_by(
            source_invoice_line_id=line.id,
            status=ServiceEntitlementStatus.active,
        )
        .one()
    )

    assert entitlement.starts_at == preview.period_start
    assert entitlement.ends_at == preview.period_end
    assert subscription.next_billing_at == original_anchor

    refund_request = PaymentRefundPreviewRequest(
        amount=invoice.total,
        reason="Reverse reviewed historical service settlement",
    )
    refund_preview = Refunds.preview(db_session, str(payment.id), refund_request)
    Refunds.process_with_evidence(
        db_session,
        str(payment.id),
        PaymentRefundRequest(
            **refund_request.model_dump(),
            preview_fingerprint=refund_preview.fingerprint,
            idempotency_key="backdated-service-refund-pytest-0001",
        ),
        commit=False,
    )
    db_session.refresh(entitlement)
    assert entitlement.status is ServiceEntitlementStatus.reversed
    assert subscription.next_billing_at == original_anchor


def test_controlled_draft_rejects_generic_edits_and_issue_time_drift(
    db_session, subscriber, subscription
):
    _prepare_subscription(db_session, subscription)
    query = _query(subscriber, subscription)
    preview = preview_backdated_service_invoice(db_session, query)
    db_session.rollback()
    result = execute_backdated_service_invoice(
        db_session, _command(query, preview.fingerprint)
    )
    invoice = db_session.get(Invoice, result.invoice_id)
    line = db_session.get(InvoiceLine, result.invoice_line_id)
    assert invoice is not None and line is not None

    with pytest.raises(InvoiceOwnerError) as owner_error:
        Invoices.seal_controlled_draft_for_owner(
            db_session,
            invoice.id,
            ControlledInvoiceDraftSeal(
                controller_owner=OWNER,
                preview_fingerprint=preview.fingerprint,
            ),
        )
    assert owner_error.value.code == "financial.invoice.controlled_draft_owner_required"

    with pytest.raises(HTTPException) as header_error:
        Invoices.update(db_session, str(invoice.id), InvoiceUpdate(memo="changed"))
    assert header_error.value.status_code == 409

    with pytest.raises(HTTPException) as line_error:
        InvoiceLines.update(
            db_session,
            str(line.id),
            InvoiceLineUpdate(unit_price=Decimal("1.00")),
        )
    assert line_error.value.status_code == 409

    invoice.memo = "unexpected direct mutation"
    db_session.flush()
    with pytest.raises(InvoiceOwnerError) as issue_error:
        Invoices.issue_draft_system(
            db_session,
            str(invoice.id),
            issuance=verified_draft_issuance(
                invoice,
                issued_at=datetime(2026, 10, 2, tzinfo=UTC),
                reason="pytest drift refusal",
            ),
            apply_available_credit=False,
            commit=False,
        )
    assert issue_error.value.code == "financial.invoice.controlled_draft_drift"


def test_preview_refuses_unproven_historical_terms(
    db_session, subscriber, subscription
):
    _prepare_subscription(db_session, subscription)
    reference = (
        db_session.query(Invoice)
        .filter(Invoice.invoice_number.like("INV-TERMS-%"))
        .one()
    )
    reference.is_active = False
    db_session.commit()

    with pytest.raises(BackdatedServiceInvoiceError) as exc_info:
        preview_backdated_service_invoice(db_session, _query(subscriber, subscription))

    assert exc_info.value.code.endswith(".historical_terms_unproven")


def test_generic_line_api_cannot_forge_backdated_service_evidence(
    db_session, subscriber
):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-FORGE-{uuid4()}",
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("0.00"),
        balance_due=Decimal("0.00"),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        InvoiceLines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Forged historical service line",
                quantity=Decimal("1.000"),
                unit_price=Decimal("52500.00"),
                amount=Decimal("52500.00"),
                metadata_={MARKER_KEY: True},
            ),
        )

    assert exc_info.value.status_code == 409


def test_existing_matching_invoice_blocks_create_and_can_be_reconstructed(
    db_session, subscriber, subscription
):
    _prepare_subscription(db_session, subscription)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-BACKDATED-TEST",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("52500.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("52500.00"),
        balance_due=Decimal("52500.00"),
        issued_at=datetime(2026, 10, 1, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        description="Generic service",
        quantity=Decimal("1.000"),
        unit_price=Decimal("52500.00"),
        amount=Decimal("52500.00"),
        is_active=True,
    )
    db_session.add(line)
    db_session.commit()

    create_preview = preview_backdated_service_invoice(
        db_session, _query(subscriber, subscription)
    )

    assert create_preview.actionable is False
    assert invoice.id in create_preview.reconstruction_candidate_ids

    query = _query(
        subscriber,
        subscription,
        action=BackdatedServiceInvoiceAction.reconstruct,
        invoice_id=invoice.id,
    )
    preview = preview_backdated_service_invoice(db_session, query)
    assert preview.actionable is True
    db_session.rollback()

    result = execute_backdated_service_invoice(
        db_session,
        _command(
            query,
            preview.fingerprint,
            scope="billing:invoice:reconstruct_backdated_service",
        ),
    )
    repaired_invoice = db_session.get(Invoice, result.invoice_id)
    repaired_line = db_session.get(InvoiceLine, line.id)

    assert repaired_invoice.billing_period_start == preview.period_start
    assert repaired_invoice.billing_period_end == preview.period_end
    assert repaired_line.subscription_id == subscription.id
    assert repaired_line.metadata_["reviewed_prepaid_sequence_ref"].startswith(
        RECONSTRUCTION_REF_PREFIX
    )
    assert (
        resolve_payment_allocation_finalization(db_session, repaired_invoice).mode
        is PaymentAllocationFinalizationMode.reviewed_document_correction
    )


def test_preview_blocks_overlapping_entitlement(db_session, subscriber, subscription):
    _prepare_subscription(db_session, subscription)
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            starts_at=datetime(2026, 6, 16, 23, tzinfo=UTC),
            ends_at=datetime(2026, 7, 16, 23, tzinfo=UTC),
            amount_funded=Decimal("52500.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.commit()

    preview = preview_backdated_service_invoice(
        db_session, _query(subscriber, subscription)
    )

    assert preview.actionable is False
    assert preview.overlapping_entitlement_ids
    assert "entitlement" in preview.reason.lower()


def test_command_rechecks_dedicated_permission(db_session, subscriber, subscription):
    _prepare_subscription(db_session, subscription)
    query = _query(subscriber, subscription)
    preview = preview_backdated_service_invoice(db_session, query)
    db_session.rollback()

    with pytest.raises(BackdatedServiceInvoiceError) as exc_info:
        execute_backdated_service_invoice(
            db_session,
            _command(query, preview.fingerprint, permission_granted=False),
        )

    assert exc_info.value.code.endswith(".permission_denied")
