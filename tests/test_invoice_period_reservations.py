"""Catalogue reservations arbitrate manual and automatic period writers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing.contracts import (
    ServiceChargeComponentV1 as ServiceChargeComponent,
)
from dotmac_billing.contracts import (
    ServiceChargePeriodV1 as ServicePeriodCharge,
)

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode
from app.schemas.billing import (
    InvoiceLineCreate,
    InvoiceLineUpdate,
    InvoiceUpdate,
    SystemInvoiceLineCreate,
)
from app.services.billing.invoices import (
    DraftInvoiceLineReplacement,
    InvoiceLines,
    InvoicePeriodReservationError,
    Invoices,
    charges_overlap,
    invoice_period_conflict,
    service_period_charge,
)

START = datetime(2030, 1, 2, tzinfo=UTC)
END = datetime(2030, 2, 2, tzinfo=UTC)


def _metadata(
    *, claimed: bool = True, start: datetime = START, end: datetime = END
) -> dict[str, object]:
    return {
        "kind": "base_subscription",
        "billing_period_start": start.isoformat(),
        "billing_period_end": end.isoformat(),
        "future_catalogue_invoice": claimed,
        "future_invoice": {"period_kind": "monthly"},
    }


def _invoice(db_session, subscriber, number: str) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=number,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("0"),
        tax_total=Decimal("0"),
        total=Decimal("0"),
        balance_due=Decimal("0"),
    )
    db_session.add(invoice)
    db_session.flush()
    return invoice


def _replacement(
    invoice, subscription, *, claimed=True, start=START, end=END, line_id=None
):
    return DraftInvoiceLineReplacement(
        line_id=line_id,
        payload=InvoiceLineCreate(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Unlimited Elite",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            metadata_=_metadata(claimed=claimed, start=start, end=end),
        ),
    )


def test_dates_are_line_scoped_and_adjacent_periods_do_not_overlap():
    subscription_id = uuid4()
    charge = service_period_charge(
        subscription_id=subscription_id,
        metadata=_metadata(),
        header_starts_at=START - timedelta(days=40),
        header_ends_at=END + timedelta(days=40),
    )
    assert charge is not None
    assert (charge.starts_at, charge.ends_at) == (START, END)
    assert not charges_overlap(
        charge, replace(charge, starts_at=END, ends_at=datetime(2030, 3, 2, tzinfo=UTC))
    )
    assert charges_overlap(charge, replace(charge, starts_at=START + timedelta(days=1)))
    assert not charges_overlap(
        charge,
        replace(charge, component=ServiceChargeComponent.addon, component_id="router"),
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"future_catalogue_invoice": "true"},
        {"billing_period_start": "bad"},
        {"billing_period_start": "2030-01-02"},
        {"billing_period_end": START.isoformat()},
        {"kind": []},
    ],
)
def test_claim_metadata_is_strict(patch):
    with pytest.raises(InvoicePeriodReservationError):
        service_period_charge(
            subscription_id=uuid4(),
            metadata={**_metadata(), **patch},
            header_starts_at=START,
            header_ends_at=END,
        )


def test_capped_pending_claim_has_no_dates_and_blocks_same_subscription():
    subscription_id = uuid4()
    pending = service_period_charge(
        subscription_id=subscription_id,
        metadata={
            "kind": "base_subscription",
            "future_catalogue_invoice": True,
            "future_invoice": {"period_kind": "capped_pending_renewal"},
        },
        header_starts_at=START,
        header_ends_at=END,
    )
    assert pending is not None and pending.pending_capped_renewal
    assert pending.starts_at is None and pending.ends_at is None
    assert charges_overlap(
        pending,
        ServicePeriodCharge(
            str(subscription_id), ServiceChargeComponent.base, "base", START, END
        ),
    )
    bound = service_period_charge(
        subscription_id=subscription_id,
        metadata={
            **_metadata(),
            "future_invoice": {"period_kind": "capped_pending_renewal"},
        },
        header_starts_at=None,
        header_ends_at=None,
    )
    assert bound is not None and not bound.pending_capped_renewal


def test_draft_claim_blocks_automatic_writer_with_document_reference(
    db_session, subscriber, subscription
):
    reserved = _invoice(db_session, subscriber, "1130")
    InvoiceLines.replace_admin_draft_lines(
        db_session, reserved.id, (_replacement(reserved, subscription),)
    )
    automatic = _invoice(db_session, subscriber, "1131")
    with pytest.raises(InvoicePeriodReservationError, match="draft invoice 1130"):
        InvoiceLines.stage_system_line(
            db_session,
            SystemInvoiceLineCreate(
                invoice_id=automatic.id,
                subscription_id=subscription.id,
                description="Automatic renewal",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                billing_line_key="scheduled-separate-key",
                metadata_=_metadata(claimed=False),
            ),
            reason="scheduled_billing_run",
        )


def test_legacy_reservation_blocks_new_catalogue_and_own_edit_is_allowed(
    db_session, subscriber, subscription
):
    reserved = _invoice(db_session, subscriber, "1130")
    InvoiceLines.replace_admin_draft_lines(
        db_session, reserved.id, (_replacement(reserved, subscription),)
    )
    line = db_session.query(InvoiceLine).filter_by(invoice_id=reserved.id).one()
    # Full replacement excludes old own snapshot, but keeps one active claim.
    InvoiceLines.replace_admin_draft_lines(
        db_session,
        reserved.id,
        (_replacement(reserved, subscription, line_id=line.id),),
    )
    other = _invoice(db_session, subscriber, "1131")
    with pytest.raises(InvoicePeriodReservationError):
        InvoiceLines.replace_admin_draft_lines(
            db_session, other.id, (_replacement(other, subscription),)
        )
    InvoiceLines.replace_admin_draft_lines(db_session, reserved.id, ())
    InvoiceLines.replace_admin_draft_lines(
        db_session, other.id, (_replacement(other, subscription),)
    )
    assert (
        db_session.query(InvoiceLine)
        .filter_by(invoice_id=other.id, is_active=True)
        .count()
        == 1
    )


def test_same_draft_duplicate_period_is_refused_before_lines_written(
    db_session, subscriber, subscription
):
    invoice = _invoice(db_session, subscriber, "1130")
    line = _replacement(invoice, subscription)
    with pytest.raises(InvoicePeriodReservationError, match="already selected"):
        InvoiceLines.replace_admin_draft_lines(db_session, invoice.id, (line, line))
    assert db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).count() == 0


def test_generic_update_cannot_remove_catalogue_claim(
    db_session, subscriber, subscription
):
    invoice = _invoice(db_session, subscriber, "1130")
    InvoiceLines.replace_admin_draft_lines(
        db_session, invoice.id, (_replacement(invoice, subscription),)
    )
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    with pytest.raises(InvoicePeriodReservationError) as denied:
        InvoiceLines.update(db_session, str(line.id), InvoiceLineUpdate(metadata_={}))
    assert denied.value.code.endswith("catalogue_line_owner_required")
    assert line.metadata_["future_catalogue_invoice"] is True


def test_invoice_less_entitlement_is_reported_even_without_exclusion(
    db_session, subscriber, subscription
):
    subscription.billing_mode = BillingMode.prepaid
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            starts_at=START,
            ends_at=END,
            amount_funded=Decimal("100"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.flush()
    charge = service_period_charge(
        subscription_id=subscription.id,
        metadata=_metadata(),
        header_starts_at=None,
        header_ends_at=None,
    )
    assert charge is not None
    conflict = invoice_period_conflict(
        db_session, account_id=subscriber.id, charge=charge
    )
    assert conflict is not None and conflict.reason == "period_covered"


def test_paid_capped_cycle_does_not_reserve_all_future_renewals(
    db_session, subscriber, subscription
):
    previous = _invoice(db_session, subscriber, "1125")
    previous.status = InvoiceStatus.paid
    previous.issued_at = START
    previous.due_at = END
    db_session.add(
        InvoiceLine(
            invoice_id=previous.id,
            subscription_id=subscription.id,
            description="Previous capped cycle",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            amount=Decimal("100"),
            metadata_=_metadata(),
        )
    )
    previous.subtotal = previous.total = Decimal("100")
    previous.balance_due = Decimal("0")
    cash = Payment(
        account_id=subscriber.id,
        amount=Decimal("100"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(cash)
    db_session.flush()
    db_session.add(
        PaymentAllocation(
            payment_id=cash.id, invoice_id=previous.id, amount=Decimal("100")
        )
    )
    db_session.flush()
    pending = service_period_charge(
        subscription_id=subscription.id,
        metadata={
            "kind": "base_subscription",
            "future_catalogue_invoice": True,
            "future_invoice": {"period_kind": "capped_pending_renewal"},
        },
        header_starts_at=None,
        header_ends_at=None,
    )
    assert pending is not None
    assert (
        invoice_period_conflict(db_session, account_id=subscriber.id, charge=pending)
        is None
    )


def test_unsettled_capped_request_blocks_second_pending_request(
    db_session, subscriber, subscription
):
    first = _invoice(db_session, subscriber, "1130")
    pending_metadata = {
        "kind": "base_subscription",
        "future_catalogue_invoice": True,
        "future_invoice": {"period_kind": "capped_pending_renewal"},
    }
    InvoiceLines.replace_admin_draft_lines(
        db_session,
        first.id,
        (
            DraftInvoiceLineReplacement(
                payload=InvoiceLineCreate(
                    invoice_id=first.id,
                    subscription_id=subscription.id,
                    description="Capped renewal",
                    unit_price=Decimal("100"),
                    metadata_=pending_metadata,
                )
            ),
        ),
    )
    pending = service_period_charge(
        subscription_id=subscription.id,
        metadata=pending_metadata,
        header_starts_at=None,
        header_ends_at=None,
    )
    assert pending is not None
    conflict = invoice_period_conflict(
        db_session, account_id=subscriber.id, charge=pending
    )
    assert conflict is not None and conflict.invoice_id == first.id


def test_raw_line_add_and_delete_cannot_bypass_catalogue_form(
    db_session, subscriber, subscription
):
    invoice = _invoice(db_session, subscriber, "1130")
    InvoiceLines.replace_admin_draft_lines(
        db_session, invoice.id, (_replacement(invoice, subscription),)
    )
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    with pytest.raises(InvoicePeriodReservationError) as add_denied:
        InvoiceLines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=invoice.id,
                description="Arbitrary price",
                unit_price=Decimal("1"),
            ),
        )
    assert add_denied.value.code.endswith("catalogue_line_owner_required")
    with pytest.raises(InvoicePeriodReservationError) as delete_denied:
        InvoiceLines.delete(db_session, str(line.id))
    assert delete_denied.value.code.endswith("catalogue_line_owner_required")
    assert line.is_active


@pytest.mark.parametrize(
    "status,blocked",
    [
        (InvoiceStatus.void, False),
        (InvoiceStatus.written_off, True),
        (InvoiceStatus.paid, True),
    ],
)
def test_void_releases_but_paid_and_written_off_periods_stay_billed(
    db_session, subscriber, subscription, status, blocked
):
    invoice = _invoice(db_session, subscriber, "1125")
    InvoiceLines.replace_admin_draft_lines(
        db_session, invoice.id, (_replacement(invoice, subscription),)
    )
    invoice.status = status
    db_session.flush()
    charge = service_period_charge(
        subscription_id=subscription.id,
        metadata=_metadata(),
        header_starts_at=None,
        header_ends_at=None,
    )
    assert charge is not None
    conflict = invoice_period_conflict(
        db_session, account_id=subscriber.id, charge=charge
    )
    assert (conflict is not None) is blocked


def test_unsettled_purchase_blocks_new_claim_even_without_invoice(
    db_session, subscriber, subscription, monkeypatch
):
    from app.services import purchased_service_coverage
    from app.services.purchased_service_coverage import PurchasedCoverage

    monkeypatch.setattr(
        purchased_service_coverage,
        "resolve_purchased_coverage",
        lambda _db, _query: PurchasedCoverage(None, True),
    )
    charge = service_period_charge(
        subscription_id=subscription.id,
        metadata=_metadata(),
        header_starts_at=None,
        header_ends_at=None,
    )
    assert charge is not None
    conflict = invoice_period_conflict(
        db_session, account_id=subscriber.id, charge=charge
    )
    assert conflict is not None and conflict.reason == "period_purchase_pending"


def test_partial_prepaid_extension_overlap_is_not_available(
    db_session, subscriber, subscription
):
    from app.models.service_extension import (
        ServiceExtension,
        ServiceExtensionEntry,
        ServiceExtensionScope,
        ServiceExtensionStatus,
    )

    subscription.billing_mode = BillingMode.prepaid
    extension = ServiceExtension(
        reason="Compensated service",
        window_start=START - timedelta(days=1),
        window_end=START,
        days=2,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscriber.id)],
        status=ServiceExtensionStatus.applied,
    )
    db_session.add(extension)
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            previous_next_billing_at=START,
            new_next_billing_at=START + timedelta(days=2),
            grant_starts_at=START,
            grant_ends_at=START + timedelta(days=2),
        )
    )
    db_session.flush()
    charge = service_period_charge(
        subscription_id=subscription.id,
        metadata=_metadata(),
        header_starts_at=None,
        header_ends_at=None,
    )
    assert charge is not None
    conflict = invoice_period_conflict(
        db_session, account_id=subscriber.id, charge=charge
    )
    assert conflict is not None and "extension" in conflict.message
    # A postpaid enforcement shield is not proof the service charge was waived.
    subscription.billing_mode = BillingMode.postpaid
    db_session.flush()
    assert (
        invoice_period_conflict(db_session, account_id=subscriber.id, charge=charge)
        is None
    )


def test_read_only_period_preview_never_flushes_pending_business_changes(
    db_session, subscriber, monkeypatch
):
    from app.services.billing.invoices import Invoices

    invoice = _invoice(db_session, subscriber, "PREVIEW-CLEAN")
    db_session.flush()
    invoice.memo = "pending unsaved decision"

    def forbidden_flush(*args, **kwargs):
        raise AssertionError("Read validation attempted a business write")

    monkeypatch.setattr(db_session, "flush", forbidden_flush)
    with pytest.raises(InvoicePeriodReservationError) as exc:
        Invoices.validate_service_period_reservations(db_session, invoice)
    assert exc.value.code == "financial.invoice.preview_requires_clean_session"
    assert invoice.memo == "pending unsaved decision"


@pytest.mark.parametrize(
    "changes",
    [
        {"memo": "Bypass complete draft review"},
        {"subtotal": Decimal("999.00")},
        {"balance_due": Decimal("1.00")},
        {"status": InvoiceStatus.issued},
    ],
)
def test_standalone_header_update_cannot_bypass_atomic_draft_authoring(
    db_session, subscriber, changes
):
    invoice = _invoice(db_session, subscriber, f"HEADER-{uuid4()}")
    before = (invoice.memo, invoice.subtotal, invoice.balance_due, invoice.status)
    with pytest.raises(InvoicePeriodReservationError) as failure:
        Invoices.update(db_session, str(invoice.id), InvoiceUpdate(**changes))
    assert failure.value.code.endswith("catalogue_draft_owner_required")
    db_session.refresh(invoice)
    assert (
        invoice.memo,
        invoice.subtotal,
        invoice.balance_due,
        invoice.status,
    ) == before
