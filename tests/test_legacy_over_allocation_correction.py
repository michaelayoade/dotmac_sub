"""Finance-reviewed return of a legacy over-allocation to account credit.

Two Splynx-era payments were allocated to one 17,500 invoice (17,500 and
18,812.50). The existing reviewed reversal refuses the legacy 18,812.50
allocation (no paired ledger evidence, and it is limited to void invoices).
This owner moves exactly that excess to account credit, only while the invoice
stays fully paid, and posts nothing because the ledger already holds the
payment as unallocated credit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.event_store import EventStore
from app.models.system_user import SystemUser
from app.schemas.billing import PaymentAllocationReversalPreviewRequest
from app.services.billing._common import get_account_credit_balance
from app.services.billing.legacy_over_allocation_correction import (
    CORRECTION_PERMISSION,
    LegacyOverAllocationBlocker,
    LegacyOverAllocationError,
    LegacyOverAllocationQuery,
    ReturnLegacyOverAllocationCommand,
    preview_legacy_over_allocation_return,
    return_legacy_over_allocation,
)
from app.services.billing.payments import (
    PaymentAllocations,
    ReviewedLegacyOverAllocationReturn,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

TOTAL = Decimal("17500.00")
EXCESS = Decimal("18812.50")
SHA = "d" * 64
PAID_AT = datetime(2026, 6, 16, 16, 53, tzinfo=UTC)


class _Fixture:
    def __init__(self, **values):
        self.__dict__.update(values)


def _staff(db, name: str = "Finance") -> SystemUser:
    user = SystemUser(
        id=uuid4(),
        first_name=name,
        last_name="Operator",
        display_name=f"{name} Operator",
        email=f"{name.lower()}-{uuid4().hex}@example.test",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def _payment(db, account, amount: Decimal, *, native: bool = False) -> Payment:
    payment = Payment(
        splynx_payment_id=None if native else 7_000_000 + int(uuid4().int % 1_000_000),
        account_id=account.id,
        amount=amount,
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=PAID_AT,
        is_active=True,
        memo="CIP CR bank transfer",
    )
    db.add(payment)
    db.flush()
    return payment


def _credit(db, account, payment: Payment, *, invoice=None) -> LedgerEntry:
    entry = LedgerEntry(
        account_id=account.id,
        invoice_id=invoice.id if invoice is not None else None,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=payment.amount,
        currency="NGN",
        memo=f"Payment {payment.id}",
        is_active=True,
        affects_customer_position=True,
        effective_date=PAID_AT,
    )
    db.add(entry)
    db.flush()
    return entry


def _case_c(db, account, *, native: bool = False) -> _Fixture:
    """INV-108193 (17,500, paid) with a 17,500 and a legacy 18,812.50 allocation."""
    invoice = Invoice(
        account_id=account.id,
        invoice_number="INV-108193",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=TOTAL,
        total=TOTAL,
        balance_due=Decimal("0.00"),
        issued_at=PAID_AT,
        paid_at=PAID_AT,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            description="Unlimited Basic",
            quantity=Decimal("1.000"),
            unit_price=TOTAL,
            amount=TOTAL,
        )
    )
    keeper_payment = _payment(db, account, TOTAL, native=native)
    excess_payment = _payment(db, account, EXCESS, native=native)
    keeper = PaymentAllocation(
        payment_id=keeper_payment.id, invoice_id=invoice.id, amount=TOTAL
    )
    excess = PaymentAllocation(
        payment_id=excess_payment.id, invoice_id=invoice.id, amount=EXCESS
    )
    db.add_all([keeper, excess])
    _credit(db, account, keeper_payment, invoice=invoice)
    credit = _credit(db, account, excess_payment)
    db.commit()
    return _Fixture(
        invoice=invoice,
        keeper=keeper,
        excess=excess,
        keeper_payment=keeper_payment,
        excess_payment=excess_payment,
        credit=credit,
    )


def _query(fixture: _Fixture, **overrides) -> LegacyOverAllocationQuery:
    values = {
        "allocation_id": fixture.excess.id,
        "expected_amount": EXCESS,
        "expected_invoice_total": TOTAL,
        "expected_remaining_settlement": TOTAL,
    }
    values.update(overrides)
    return LegacyOverAllocationQuery(**values)


def _confirm(
    db,
    query,
    user,
    *,
    fingerprint=None,
    key=None,
    granted=True,
    scope=CORRECTION_PERMISSION,
):
    user_id = user.id
    if fingerprint is None:
        fingerprint = preview_legacy_over_allocation_return(db, query).fingerprint
    context = CommandContext.system(
        actor=f"user:{user_id}",
        scope=scope,
        reason="finance-reviewed legacy over-allocation return test",
        idempotency_key=key or f"confirm-{uuid4()}",
    )
    db.commit()  # adapters hand owners a transaction-free session
    return return_legacy_over_allocation(
        db,
        ReturnLegacyOverAllocationCommand(
            query=query,
            preview_fingerprint=fingerprint,
            reason="Finance decided the 18,812.50 over-allocation is account credit",
            evidence_reference="finance-ticket-FIN-88",
            evidence_sha256=SHA,
            reviewed_by=user_id,
            permission_granted=granted,
        ),
        context=context,
    )


def test_existing_reviewed_reversal_refuses_the_legacy_allocation(
    db_session, subscriber_account
):
    fixture = _case_c(db_session, subscriber_account)

    with pytest.raises(HTTPException) as refused:
        PaymentAllocations.preview_reviewed_reversal(
            db_session,
            PaymentAllocationReversalPreviewRequest(allocation_id=fixture.excess.id),
        )

    assert "lacks paired ledger evidence" in str(refused.value.detail)


def test_preview_proves_the_exact_excess_and_the_empty_ledger_posting(
    db_session, subscriber_account
):
    fixture = _case_c(db_session, subscriber_account)
    credit_before = get_account_credit_balance(db_session, str(subscriber_account.id))

    preview = preview_legacy_over_allocation_return(db_session, _query(fixture))

    assert preview.actionable, preview.blockers
    assert preview.invoice_number == "INV-108193"
    assert preview.allocation_amount == EXCESS
    assert (preview.settled_before, preview.settled_after) == (
        TOTAL + EXCESS,
        TOTAL,
    )
    assert preview.invoice_total == TOTAL
    assert preview.invoice_status == "paid"
    assert [row.allocation_id for row in preview.remaining_allocations] == [
        fixture.keeper.id
    ]
    assert (
        preview.payment_allocation_unallocated_before,
        preview.payment_allocation_unallocated_after,
    ) == (
        Decimal("0.00"),
        EXCESS,
    )
    assert preview.ledger.ledger_entry_id == fixture.credit.id
    assert preview.ledger.consumption_debit_count == 0
    assert preview.ledger_postings == ()
    assert preview.account_credit_before == preview.account_credit_after
    assert preview.account_credit_before == credit_before
    again = preview_legacy_over_allocation_return(db_session, _query(fixture))
    assert again.fingerprint == preview.fingerprint
    db_session.refresh(fixture.excess)
    assert fixture.excess.is_active is True


def test_confirm_returns_the_excess_without_posting_or_touching_the_invoice(
    db_session, subscriber_account
):
    fixture = _case_c(db_session, subscriber_account)
    staff = _staff(db_session)
    query = _query(fixture)
    ids = (fixture.excess.id, fixture.keeper.id, fixture.invoice.id)
    excess_payment_id = fixture.excess_payment.id
    credit_before = get_account_credit_balance(db_session, str(subscriber_account.id))
    ledger_count = db_session.query(LedgerEntry).count()

    result = _confirm(db_session, query, staff, key="legacy-c-1")

    assert result.replayed is False
    assert result.amount == EXCESS
    assert result.account_credit_ledger_entry_id == fixture.credit.id
    db_session.expire_all()
    excess = db_session.get(PaymentAllocation, ids[0])
    keeper = db_session.get(PaymentAllocation, ids[1])
    invoice = db_session.get(Invoice, ids[2])
    assert excess.is_active is False
    assert excess.reversed_at is not None
    assert excess.reversal_idempotency_key == "legacy-c-1"
    assert excess.reversal_preview_fingerprint == result.preview_fingerprint
    assert excess.reversal_actor_id == staff.id
    assert excess.reversal_ledger_entry_id is None
    assert excess.reversal_consumption_ledger_entry_id is None
    assert keeper.is_active is True
    assert (invoice.status, invoice.total, invoice.balance_due) == (
        InvoiceStatus.paid,
        TOTAL,
        Decimal("0.00"),
    )
    # No ledger posting: the payment was already unallocated account credit.
    assert db_session.query(LedgerEntry).count() == ledger_count
    assert (
        get_account_credit_balance(db_session, str(subscriber_account.id))
        == credit_before
    )
    assert db_session.get(Payment, excess_payment_id).amount == EXCESS
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "return_legacy_over_allocation_to_account_credit")
        .one()
    )
    assert audit.entity_id == str(ids[0])
    assert audit.actor_id == str(staff.id)
    assert audit.metadata_["economic_delta"] == "0.00"
    assert audit.metadata_["ledger_postings"] == []
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "payment_allocation.over_allocation_returned")
        .one()
    )
    assert event.payload["allocation_id"] == str(ids[0])
    # The invoice is no longer over-settled.
    after = preview_legacy_over_allocation_return(db_session, _query(fixture))
    assert LegacyOverAllocationBlocker.allocation_inactive in after.blockers


def test_replay_conflict_stale_and_permission(db_session, subscriber_account):
    fixture = _case_c(db_session, subscriber_account)
    staff = _staff(db_session)
    query = _query(fixture)
    fingerprint = preview_legacy_over_allocation_return(db_session, query).fingerprint
    first = _confirm(db_session, query, staff, fingerprint=fingerprint, key="same")

    replay = _confirm(db_session, query, staff, fingerprint=fingerprint, key="same")

    assert replay.replayed is True
    assert replay.correction_id == first.correction_id
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "return_legacy_over_allocation_to_account_credit")
        .count()
        == 1
    )
    other = _staff(db_session, "Other")
    with pytest.raises(LegacyOverAllocationError) as conflict:
        _confirm(db_session, query, other, fingerprint=fingerprint, key="same")
    assert conflict.value.code.endswith("idempotency_conflict")
    with pytest.raises(LegacyOverAllocationError) as denied:
        _confirm(db_session, query, staff, fingerprint=fingerprint, granted=False)
    assert denied.value.code.endswith("permission_denied")
    with pytest.raises(LegacyOverAllocationError) as wrong_scope:
        _confirm(
            db_session,
            query,
            staff,
            fingerprint=fingerprint,
            scope="billing:ledger:write",
        )
    assert wrong_scope.value.code.endswith("permission_denied")
    with pytest.raises(LegacyOverAllocationError) as stale:
        _confirm(db_session, query, staff, fingerprint="0" * 64, key="stale")
    assert stale.value.code.endswith("stale_preview")
    # A fresh key cannot return it twice.
    blocked = preview_legacy_over_allocation_return(db_session, query)
    with pytest.raises(LegacyOverAllocationError) as again:
        _confirm(db_session, query, staff, fingerprint=blocked.fingerprint, key="twice")
    assert again.value.code.endswith("not_actionable")


@pytest.mark.parametrize(
    "mutation, blocker",
    [
        ("ledger_link", LegacyOverAllocationBlocker.allocation_has_ledger_evidence),
        ("native_key", LegacyOverAllocationBlocker.allocation_has_native_evidence),
        ("settlement", LegacyOverAllocationBlocker.payment_has_settlement),
        ("refund", LegacyOverAllocationBlocker.payment_not_eligible),
        ("failed_payment", LegacyOverAllocationBlocker.payment_not_eligible),
        ("consumption", LegacyOverAllocationBlocker.consumption_debit_present),
        ("no_credit", LegacyOverAllocationBlocker.payment_credit_ledger_missing),
        ("extra_entry", LegacyOverAllocationBlocker.payment_ledger_evidence_not_exact),
        ("elsewhere", LegacyOverAllocationBlocker.payment_allocated_elsewhere),
        ("invoice_open", LegacyOverAllocationBlocker.invoice_not_paid),
        ("underpaid", LegacyOverAllocationBlocker.invoice_would_not_stay_paid),
        ("not_exact", LegacyOverAllocationBlocker.allocation_not_exact_excess),
        ("no_account", LegacyOverAllocationBlocker.payment_not_eligible),
    ],
)
def test_preview_blocks_anything_that_is_not_exactly_the_legacy_excess(
    db_session, subscriber_account, mutation, blocker
):
    fixture = _case_c(db_session, subscriber_account)
    excess_payment = fixture.excess_payment
    if mutation == "ledger_link":
        fixture.excess.ledger_entry_id = fixture.credit.id
    elif mutation == "native_key":
        fixture.excess.idempotency_key = "native-key"
    elif mutation == "settlement":
        db_session.add(
            PaymentSettlement(
                payment_id=excess_payment.id,
                amount=EXCESS,
                currency="NGN",
                unallocated_amount=Decimal("0.00"),
                origin=PaymentSettlementOrigin.manual,
                idempotency_key=f"settle-{uuid4()}",
                preview_fingerprint="e" * 64,
            )
        )
    elif mutation == "refund":
        excess_payment.refunded_amount = Decimal("100.00")
    elif mutation == "failed_payment":
        excess_payment.status = PaymentStatus.failed
    elif mutation == "consumption":
        db_session.add(
            LedgerEntry(
                account_id=subscriber_account.id,
                payment_id=excess_payment.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.other,
                amount=EXCESS,
                currency="NGN",
                memo=f"Payment allocation account-credit consumption: {fixture.invoice.id}",
                is_active=True,
                affects_customer_position=False,
            )
        )
    elif mutation == "no_credit":
        fixture.credit.is_active = False
    elif mutation == "extra_entry":
        _credit(db_session, subscriber_account, excess_payment)
    elif mutation == "elsewhere":
        other_invoice = Invoice(
            account_id=subscriber_account.id,
            invoice_number="INV-OTHER",
            status=InvoiceStatus.paid,
            currency="NGN",
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("0.00"),
        )
        db_session.add(other_invoice)
        db_session.flush()
        db_session.add(
            PaymentAllocation(
                payment_id=excess_payment.id,
                invoice_id=other_invoice.id,
                amount=Decimal("100.00"),
            )
        )
    elif mutation == "invoice_open":
        fixture.invoice.status = InvoiceStatus.issued
        fixture.invoice.balance_due = Decimal("5.00")
    elif mutation == "underpaid":
        fixture.keeper.amount = Decimal("10000.00")
    elif mutation == "not_exact":
        fixture.keeper.amount = Decimal("20000.00")
    elif mutation == "no_account":
        excess_payment.account_id = None
    db_session.commit()

    query = _query(fixture)
    if mutation == "underpaid":
        query = _query(fixture, expected_remaining_settlement=Decimal("10000.00"))
    if mutation == "not_exact":
        query = _query(fixture, expected_remaining_settlement=Decimal("20000.00"))
    preview = preview_legacy_over_allocation_return(db_session, query)

    assert blocker in preview.blockers
    assert preview.actionable is False
    staff = _staff(db_session)
    with pytest.raises(LegacyOverAllocationError) as refused:
        _confirm(db_session, query, staff, fingerprint=preview.fingerprint)
    assert refused.value.code.endswith("not_actionable")
    db_session.expire_all()
    assert db_session.get(PaymentAllocation, fixture.excess.id).is_active is True


def test_restated_amounts_must_match_the_stored_amounts(db_session, subscriber_account):
    fixture = _case_c(db_session, subscriber_account)

    wrong = preview_legacy_over_allocation_return(
        db_session,
        _query(
            fixture,
            expected_amount=Decimal("18812.00"),
            expected_invoice_total=Decimal("17000.00"),
            expected_remaining_settlement=Decimal("1.00"),
        ),
    )

    assert {
        LegacyOverAllocationBlocker.expected_amount_mismatch,
        LegacyOverAllocationBlocker.expected_invoice_total_mismatch,
        LegacyOverAllocationBlocker.expected_remaining_mismatch,
    } <= set(wrong.blockers)


def test_the_participant_is_unusable_outside_the_owner_command(
    db_session, subscriber_account
):
    fixture = _case_c(db_session, subscriber_account)

    with pytest.raises(DomainError) as rejected:
        PaymentAllocations.stage_reviewed_legacy_over_allocation_return_for_owner(
            db_session,
            ReviewedLegacyOverAllocationReturn(
                allocation_id=fixture.excess.id,
                payment_id=fixture.excess_payment.id,
                invoice_id=fixture.invoice.id,
                expected_amount=EXCESS,
                reviewed_by=uuid4(),
                preview_fingerprint="f" * 64,
                idempotency_key="direct",
                reason="direct call",
            ),
        )

    assert rejected.value.code == (
        "financial.payments.legacy_over_allocation_return_rejected"
    )
    db_session.refresh(fixture.excess)
    assert fixture.excess.is_active is True


def test_sub_native_allocation_without_legacy_provenance_is_refused(
    db_session, subscriber_account
):
    """Missing ledger fields alone also describe Sub-native allocations."""
    fixture = _case_c(db_session, subscriber_account, native=True)

    preview = preview_legacy_over_allocation_return(db_session, _query(fixture))

    assert LegacyOverAllocationBlocker.allocation_not_legacy_provenance in (
        preview.blockers
    )
    assert preview.actionable is False


def test_splynx_provenance_on_the_payment_is_accepted(db_session, subscriber_account):
    fixture = _case_c(db_session, subscriber_account, native=True)
    fixture.excess_payment.splynx_payment_id = 42
    db_session.commit()

    preview = preview_legacy_over_allocation_return(db_session, _query(fixture))

    assert LegacyOverAllocationBlocker.allocation_not_legacy_provenance not in (
        preview.blockers
    )


@pytest.mark.parametrize("where", ["memo", "receipt_number", "external_id"])
def test_possible_duplicate_payment_reference_blocks(
    db_session, subscriber_account, where
):
    fixture = _case_c(db_session, subscriber_account)
    session_id = "100004260616165300123456789012"
    fixture.excess_payment.memo = f"NIP transfer session {session_id}"
    other = _payment(db_session, subscriber_account, Decimal("500.00"))
    if where == "memo":
        other.memo = f"duplicate entry {session_id}"
    elif where == "receipt_number":
        other.receipt_number = session_id
    else:
        other.external_id = session_id
    db_session.commit()

    preview = preview_legacy_over_allocation_return(db_session, _query(fixture))

    assert LegacyOverAllocationBlocker.possible_duplicate_payment_reference in (
        preview.blockers
    )
    assert preview.actionable is False


def test_distinct_references_do_not_block(db_session, subscriber_account):
    fixture = _case_c(db_session, subscriber_account)
    fixture.excess_payment.memo = "NIP transfer session 100004260616165300123456789012"
    fixture.keeper_payment.memo = "NIP transfer session 100004260616170000987654321098"
    db_session.commit()

    preview = preview_legacy_over_allocation_return(db_session, _query(fixture))

    assert preview.actionable, preview.blockers
