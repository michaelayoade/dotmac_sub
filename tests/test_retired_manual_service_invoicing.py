"""Fast unit retirement and historical-record compatibility evidence."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import (
    InvoiceLine,
    InvoiceStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.schemas.billing import (
    InvoiceLineCreate,
    InvoiceUpdate,
    PaymentRefundPreviewRequest,
    PaymentRefundRequest,
)
from app.services.billing.invoices import (
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
    _assert_controlled_draft_ready_for_issue,
    _controlled_draft_document_fingerprint,
    is_controlled_invoice_draft,
)
from app.services.billing.payments import (
    PaymentAllocationFinalizationMode,
    Refunds,
    resolve_payment_allocation_finalization,
)
from app.web.admin.billing_invoices import router
from tests.test_payment_allocation_settlement_consequence import (
    PERIOD_END,
    PERIOD_START,
    _allocate,
    _prepaid_invoice,
    _settled_payment,
    at,
)


def test_retired_web_actions_are_absent_and_generic_authoring_remains() -> None:
    paths = {route.path for route in router.routes}
    assert not any("backdated-service" in path for path in paths)
    assert not any("subscription-service" in path for path in paths)
    assert not any("generate-from-subscription" in path for path in paths)
    assert "/billing/invoices/new" in paths


@pytest.mark.parametrize("reconstructed", [False, True])
def test_existing_historical_document_keeps_settlement_semantics(
    db_session: Session,
    subscriber: Subscriber,
    subscription: Subscription,
    reconstructed: bool,
) -> None:
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    metadata = dict(line.metadata_)
    if reconstructed:
        metadata["reviewed_prepaid_sequence_ref"] = "backdated-service-invoicing:legacy"
    else:
        metadata["backdated_service_invoice"] = True
    line.metadata_ = metadata
    subscription.next_billing_at = datetime(2027, 1, 1, tzinfo=UTC)
    db_session.commit()
    anchor = at(subscription.next_billing_at)
    status = subscription.status

    decision = resolve_payment_allocation_finalization(db_session, invoice)
    assert (
        decision.mode is PaymentAllocationFinalizationMode.reviewed_document_correction
    )
    payment = _settled_payment(db_session, subscriber)
    _allocate(db_session, payment, invoice)
    db_session.refresh(invoice)
    db_session.refresh(subscription)
    assert invoice.status is InvoiceStatus.paid
    assert at(subscription.next_billing_at) == anchor
    assert subscription.status is status
    entitlement = (
        db_session.query(ServiceEntitlement)
        .filter_by(source_invoice_line_id=line.id)
        .one()
    )
    assert at(entitlement.starts_at) == PERIOD_START
    assert at(entitlement.ends_at) == PERIOD_END
    request = PaymentRefundPreviewRequest(
        amount=invoice.total, reason="Reverse existing historical settlement"
    )
    preview = Refunds.preview(db_session, str(payment.id), request)
    Refunds.process_with_evidence(
        db_session,
        str(payment.id),
        PaymentRefundRequest(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key="retired-historical-refund-test-0001",
        ),
        commit=False,
    )
    db_session.refresh(entitlement)
    db_session.refresh(subscription)
    assert entitlement.status is ServiceEntitlementStatus.reversed
    assert at(subscription.next_billing_at) == anchor
    assert subscription.status is status


def test_generic_writer_cannot_create_retired_historical_evidence(
    db_session: Session, subscriber: Subscriber, subscription: Subscription
) -> None:
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    invoice.status = InvoiceStatus.draft
    db_session.commit()
    with pytest.raises(HTTPException) as denied:
        InvoiceLines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Retired historical evidence",
                unit_price=1,
                quantity=1,
                metadata_={"backdated_service_invoice": True},
            ),
        )
    assert denied.value.status_code == 409
    assert db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).count() == 1


def test_existing_controlled_draft_keeps_its_seal_and_edit_refusal(
    db_session: Session, subscriber: Subscriber, subscription: Subscription
) -> None:
    invoice = _prepaid_invoice(db_session, subscriber, subscription)
    invoice.status = InvoiceStatus.draft
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    controller = "financial.backdated_service_invoicing"
    preview = "a" * 64
    # Arrange the persisted document format produced by the retired owner.
    invoice.metadata_ = {
        "controlled_draft": {
            "schema_version": 1,
            "controller_owner": controller,
            "preview_fingerprint": preview,
            "document_fingerprint": _controlled_draft_document_fingerprint(
                invoice,
                (line,),
                controller_owner=controller,
                preview_fingerprint=preview,
            ),
            "line_ids": [str(line.id)],
        }
    }
    db_session.commit()
    assert is_controlled_invoice_draft(invoice)
    _assert_controlled_draft_ready_for_issue(db_session, invoice)
    with pytest.raises(HTTPException) as denied:
        Invoices.update(db_session, str(invoice.id), InvoiceUpdate(memo="Changed"))
    assert denied.value.status_code == 409
    invoice.memo = "Unexpected direct mutation"
    db_session.flush()
    with pytest.raises(InvoiceOwnerError) as drift:
        _assert_controlled_draft_ready_for_issue(db_session, invoice)
    assert drift.value.code == "financial.invoice.controlled_draft_drift"
