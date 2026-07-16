"""Preview/confirmation evidence for debit-only account adjustments."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import billing as billing_api
from app.models.audit import AuditActorType, AuditEvent
from app.models.billing import (
    AccountAdjustment,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.schemas.billing import (
    AccountAdjustmentConfirm,
    AccountAdjustmentPreviewRequest,
    AccountAdjustmentReversalConfirm,
    AccountAdjustmentReversalPreviewRequest,
    LedgerEntryCreate,
)
from app.services.billing._common import get_account_credit_balance
from app.services.billing.adjustments import AccountAdjustments


def _fund(db_session, subscriber, amount: Decimal) -> LedgerEntry:
    entry = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        category=LedgerCategory.top_up,
        amount=amount,
        currency="NGN",
        memo="Confirmed prepaid funding",
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def _request(subscriber, *, amount: Decimal = Decimal("2500.00")):
    return AccountAdjustmentPreviewRequest(
        account_id=subscriber.id,
        category=LedgerCategory.custom_service,
        amount=amount,
        currency="NGN",
        memo="Manual service debit",
        reason="Operator-confirmed service adjustment",
    )


def _confirm(db_session, request, fingerprint, *, key="adjustment-test-key-0001"):
    return AccountAdjustments.confirm(
        db_session,
        AccountAdjustmentConfirm(
            **request.model_dump(),
            preview_fingerprint=fingerprint,
            idempotency_key=key,
        ),
        actor_type=AuditActorType.user,
        actor_id="test-operator",
    )


def test_adjustment_preview_and_confirmation_link_exact_debit(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)

    preview = AccountAdjustments.preview(db_session, request)

    assert preview.prepaid_funding_before == Decimal("5000.00")
    assert preview.prepaid_funding_after == Decimal("2500.00")
    assert preview.postpaid_receivables == Decimal("0.00")
    assert preview.collection_blocking_balance == Decimal("0.00")
    assert preview.ledger_entry_type == LedgerEntryType.debit
    assert preview.ledger_source == LedgerSource.adjustment
    assert preview.ledger_amount == Decimal("2500.00")
    assert preview.access_consequence == "none_adjustment_only"
    assert preview.allowed is True

    result = _confirm(db_session, request, preview.fingerprint)

    assert result.adjustment.ledger_entry_id == result.ledger_entry.id
    assert result.ledger_entry.entry_type == LedgerEntryType.debit
    assert result.ledger_entry.source == LedgerSource.adjustment
    assert result.ledger_entry.category == LedgerCategory.custom_service
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "2500.00"
    )
    audit = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "account_adjustment",
            AuditEvent.entity_id == str(result.adjustment.id),
            AuditEvent.action == "confirm",
        )
        .one()
    )
    assert audit.metadata_["ledger_entry_id"] == str(result.ledger_entry.id)
    assert audit.metadata_["prepaid_funding_before"] == "5000.00"
    assert audit.metadata_["prepaid_funding_after"] == "2500.00"


def test_adjustment_confirmation_rejects_stale_financial_state(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    preview = AccountAdjustments.preview(db_session, request)
    _fund(db_session, subscriber, Decimal("500.00"))

    with pytest.raises(HTTPException, match="preview again") as exc:
        _confirm(db_session, request, preview.fingerprint)

    assert exc.value.status_code == 409
    assert db_session.query(AccountAdjustment).count() == 0
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "5500.00"
    )


def test_adjustment_rejects_insufficient_funding_without_a_write(
    db_session, subscriber
):
    _fund(db_session, subscriber, Decimal("1000.00"))
    request = _request(subscriber)
    preview = AccountAdjustments.preview(db_session, request)

    assert preview.allowed is False
    assert preview.rejection_reason == "insufficient_prepaid_funding"
    assert preview.shortfall == Decimal("1500.00")
    with pytest.raises(HTTPException) as exc:
        _confirm(db_session, request, preview.fingerprint)

    assert exc.value.status_code == 402
    assert db_session.query(AccountAdjustment).count() == 0
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1000.00"
    )


def test_adjustment_confirmation_replays_without_a_second_debit(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    preview = AccountAdjustments.preview(db_session, request)
    first = _confirm(db_session, request, preview.fingerprint)

    replay = _confirm(db_session, request, preview.fingerprint)

    assert replay.replayed is True
    assert replay.adjustment.id == first.adjustment.id
    assert replay.ledger_entry.id == first.ledger_entry.id
    assert replay.preview.prepaid_funding_before == Decimal("5000.00")
    assert replay.preview.prepaid_funding_after == Decimal("2500.00")
    assert db_session.query(AccountAdjustment).count() == 1
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.source == LedgerSource.adjustment)
        .count()
        == 1
    )


def test_adjustment_reversal_is_previewed_idempotent_and_structurally_linked(
    db_session, subscriber
):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    debit_preview = AccountAdjustments.preview(db_session, request)
    debit = _confirm(db_session, request, debit_preview.fingerprint)
    reversal_request = AccountAdjustmentReversalPreviewRequest(
        reason="Operator-confirmed correction"
    )
    preview = AccountAdjustments.preview_reversal(
        db_session, str(debit.adjustment.id), reversal_request
    )

    result = AccountAdjustments.confirm_reversal(
        db_session,
        str(debit.adjustment.id),
        AccountAdjustmentReversalConfirm(
            reason=reversal_request.reason,
            preview_fingerprint=preview.fingerprint,
            idempotency_key="adjustment-reversal-test-0001",
        ),
        actor_type=AuditActorType.user,
        actor_id="test-operator",
    )

    assert result.ledger_entry.entry_type == LedgerEntryType.credit
    assert result.ledger_entry.category == LedgerCategory.custom_service
    assert result.ledger_entry.reversal_of_entry_id == debit.ledger_entry.id
    assert result.adjustment.reversal_ledger_entry_id == result.ledger_entry.id
    assert result.adjustment.reversal_prepaid_funding_before == Decimal("2500.00")
    assert result.adjustment.reversal_prepaid_funding_after == Decimal("5000.00")
    assert result.preview.access_consequence == "none_adjustment_reversal_only"
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "5000.00"
    )

    replay = AccountAdjustments.confirm_reversal(
        db_session,
        str(debit.adjustment.id),
        AccountAdjustmentReversalConfirm(
            reason=reversal_request.reason,
            preview_fingerprint=preview.fingerprint,
            idempotency_key="adjustment-reversal-test-0001",
        ),
    )
    assert replay.replayed is True
    assert replay.ledger_entry.id == result.ledger_entry.id
    assert replay.preview.prepaid_funding_before == Decimal("2500.00")
    assert replay.preview.prepaid_funding_after == Decimal("5000.00")
    assert (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == str(debit.adjustment.id),
            AuditEvent.action == "reverse",
        )
        .count()
        == 1
    )


def test_generic_ledger_write_and_reversal_api_are_gated(db_session, subscriber):
    with pytest.raises(HTTPException, match="Direct ledger posting is disabled"):
        billing_api.create_ledger_entry(
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                category=LedgerCategory.other,
                amount=Decimal("1.00"),
                currency="NGN",
                memo="Bypass attempt",
            ),
            db_session,
        )
    with pytest.raises(HTTPException, match="Direct ledger reversal is disabled"):
        billing_api.reverse_ledger_entry("unused", None, db_session)
