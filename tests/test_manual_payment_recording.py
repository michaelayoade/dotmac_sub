from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.billing import Payment, PaymentStatus
from app.models.event_store import EventStore
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.schemas.billing import (
    ManualPaymentRecordingConfirm,
    ManualPaymentRecordingPreviewRequest,
)
from app.services import manual_payment_recording
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _request(subscriber, *, reference: str | None = "BANK-REF-001"):
    return ManualPaymentRecordingPreviewRequest(
        account_id=subscriber.id,
        amount=Decimal("18000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=reference,
        auto_allocate=False,
    )


def _payment(db, subscriber, *, reference: str, amount: str = "18000.00"):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=reference,
        is_active=True,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def _proof(db, subscriber, *, reference: str, amount: str = "18000.00"):
    proof = PaymentProof(
        account_id=subscriber.id,
        submitted_by=subscriber.id,
        amount=Decimal(amount),
        currency="NGN",
        reference=reference,
        file_path=f"uploads/payment_proofs/{uuid4().hex}.png",
        status=PaymentProofStatus.submitted,
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    return proof


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="user:test-finance-operator",
        scope=manual_payment_recording.MANUAL_PAYMENT_RECORDING_SCOPE,
        reason="Record distinct bank receipt after duplicate evidence review.",
        idempotency_key=key,
    )


def _confirm(request, preview, *, key: str, acknowledged: bool = False):
    return ManualPaymentRecordingConfirm(
        **request.model_dump(),
        preview_fingerprint=preview.payment_preview.fingerprint,
        control_fingerprint=preview.control_fingerprint,
        duplicate_risk_acknowledged=acknowledged,
        idempotency_key=key,
    )


def test_succeeded_manual_payment_requires_reference(db_session, subscriber):
    with pytest.raises(DomainError) as caught:
        manual_payment_recording.preview_manual_payment_recording(
            db_session, _request(subscriber, reference=None)
        )

    assert caught.value.code.endswith(".reference_required")


def test_existing_account_reference_is_a_hard_conflict(db_session, subscriber):
    existing = _payment(db_session, subscriber, reference=" bank-ref-001 ")

    with pytest.raises(DomainError) as caught:
        manual_payment_recording.preview_manual_payment_recording(
            db_session, _request(subscriber, reference="BANK-REF-001")
        )

    assert caught.value.code.endswith(".reference_already_recorded")
    assert caught.value.details["payment_id"] == str(existing.id)


def test_submitted_proof_reference_is_a_hard_conflict(db_session, subscriber):
    proof = _proof(db_session, subscriber, reference="bank-ref-001")

    with pytest.raises(DomainError) as caught:
        manual_payment_recording.preview_manual_payment_recording(
            db_session, _request(subscriber, reference=" BANK-REF-001 ")
        )

    assert caught.value.code.endswith(".reference_has_submitted_proof")
    assert caught.value.details["proof_id"] == str(proof.id)


def test_same_amount_evidence_warns_and_requires_acknowledgement(
    db_session, subscriber
):
    payment = _payment(db_session, subscriber, reference="OLDER-PAYMENT")
    proof = _proof(db_session, subscriber, reference="SUBMITTED-PROOF")
    request = _request(subscriber)
    preview = manual_payment_recording.preview_manual_payment_recording(
        db_session, request
    )

    assert preview.requires_duplicate_acknowledgement is True
    assert {risk.evidence_id for risk in preview.duplicate_risks} == {
        payment.id,
        proof.id,
    }

    key = "manual-payment-duplicate-risk-0001"
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(DomainError) as caught:
        manual_payment_recording.confirm_manual_payment_recording(
            db_session,
            context=_context(key),
            request=_confirm(request, preview, key=key),
        )
    assert caught.value.code.endswith(".duplicate_risk_acknowledgement_required")


def test_confirm_records_reference_audit_event_and_replays_once(db_session, subscriber):
    request = _request(subscriber, reference="DISTINCT-TRANSFER-001")
    preview = manual_payment_recording.preview_manual_payment_recording(
        db_session, request
    )
    key = "manual-payment-recording-replay-0001"
    command = _confirm(request, preview, key=key)

    db_session_adapter.release_read_transaction(db_session)
    first = manual_payment_recording.confirm_manual_payment_recording(
        db_session,
        context=_context(key),
        request=command,
    )
    db_session_adapter.release_read_transaction(db_session)
    replay = manual_payment_recording.confirm_manual_payment_recording(
        db_session,
        context=_context(key),
        request=command,
    )

    assert replay.idempotent_replay is True
    assert replay.payment_result.payment.id == first.payment_result.payment.id
    assert replay.payment_result.payment.external_id == "DISTINCT-TRANSFER-001"
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "confirm_manual_payment_recording")
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "manual_payment.recorded")
        .count()
        == 1
    )


def test_confirmation_rejects_duplicate_evidence_added_after_preview(
    db_session, subscriber
):
    request = _request(subscriber, reference="NEW-TRANSFER")
    preview = manual_payment_recording.preview_manual_payment_recording(
        db_session, request
    )
    _proof(db_session, subscriber, reference="OTHER-PROOF")
    key = "manual-payment-stale-control-0001"
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(DomainError) as caught:
        manual_payment_recording.confirm_manual_payment_recording(
            db_session,
            context=_context(key),
            request=_confirm(request, preview, key=key, acknowledged=True),
        )

    assert caught.value.code.endswith(".stale_duplicate_evidence")
