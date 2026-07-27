"""Repair rejected receipts whose Deposit Account Credit intent stayed submitted."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.audit import AuditEvent
from app.models.billing import TopupIntent
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services.account_credit_deposits import AccountCreditDeposits
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.payment_proofs import (
    REJECTED_INTENT_REPAIR_BATCH_AUDIT_ACTION,
    REJECTED_INTENT_REPAIR_ITEM_AUDIT_ACTION,
    REPAIR_SCOPE,
    RejectedIntentRepairClassification,
    RepairRejectedIntentCommand,
    preview_rejected_deposit_intent_repairs,
    repair_rejected_deposit_intents,
)
from app.services.topup_intents import TopupIntentStatus


def _stale_pair(db_session, subscriber, *, proof_amount="14000.00"):
    proof = PaymentProof(
        account_id=subscriber.id,
        submitted_by=subscriber.id,
        amount=Decimal(proof_amount),
        currency="NGN",
        reference=f"TRF-{uuid4().hex[:12].upper()}",
        file_path="uploads/payment_proofs/rejected-repair.png",
        status=PaymentProofStatus.rejected,
        review_notes="Receipt could not be matched",
        verified_by="staff-1",
    )
    db_session.add(proof)
    db_session.flush()
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=proof.reference,
        provider_type="direct_bank_transfer",
        currency="NGN",
        requested_amount=Decimal("14000.00"),
        status=TopupIntentStatus.submitted.value,
        purpose="account_credit_deposit",
        allocation_policy="credit_only",
        credit_application_policy="pay_eligible_invoices",
        policy_version=1,
        preview_fingerprint="b" * 64,
        idempotency_key=f"repair-test-{uuid4()}",
        channel="customer_selfcare",
        created_by=str(subscriber.id),
        metadata_={
            "payment_method": "bank_transfer",
            "payment_flow": "account_topup",
            "payment_proof_id": str(proof.id),
        },
    )
    db_session.add(intent)
    db_session.commit()
    return proof, intent


def _command(preview):
    return RepairRejectedIntentCommand(
        context=CommandContext.system(
            actor="test:finance-operator",
            scope=REPAIR_SCOPE,
            reason="Repair regression-created stale rejected deposit intents",
            idempotency_key=preview.fingerprint,
        ),
        preview_fingerprint=preview.fingerprint,
        target="test-db",
        repairs=preview.repairs,
    )


def test_preview_is_read_only_and_classifies_exact_rejected_pair(
    db_session, subscriber
):
    proof, intent = _stale_pair(db_session, subscriber)

    preview = preview_rejected_deposit_intent_repairs(
        db_session,
        observed_at=datetime(2026, 7, 27, tzinfo=UTC),
    )

    assert len(preview.candidates) == 1
    assert preview.candidates[0].classification is (
        RejectedIntentRepairClassification.eligible
    )
    assert preview.repairs[0].intent_id == intent.id
    assert preview.repairs[0].proof_id == proof.id
    assert not db_session.dirty
    db_session.refresh(intent)
    assert intent.status == TopupIntentStatus.submitted.value


def test_apply_repairs_exact_pairs_records_audit_and_is_idempotent(
    db_session, subscriber
):
    proof, intent = _stale_pair(db_session, subscriber)
    preview = preview_rejected_deposit_intent_repairs(db_session)
    command = _command(preview)
    db_session_adapter.release_read_transaction(db_session)

    outcome = repair_rejected_deposit_intents(db_session, command)

    assert outcome.applied_count == 1
    assert outcome.already_applied is False
    db_session.expire_all()
    persisted = db_session.get(TopupIntent, intent.id)
    assert persisted is not None
    assert persisted.status == TopupIntentStatus.rejected.value
    assert persisted.metadata_["rejected_payment_proof_id"] == str(proof.id)
    assert (
        AccountCreditDeposits.active_request(
            db_session,
            account_id=subscriber.id,
        )
        is None
    )
    actions = {
        row.action
        for row in db_session.query(AuditEvent)
        .filter(
            AuditEvent.action.in_(
                {
                    REJECTED_INTENT_REPAIR_ITEM_AUDIT_ACTION,
                    REJECTED_INTENT_REPAIR_BATCH_AUDIT_ACTION,
                }
            )
        )
        .all()
    }
    assert actions == {
        REJECTED_INTENT_REPAIR_ITEM_AUDIT_ACTION,
        REJECTED_INTENT_REPAIR_BATCH_AUDIT_ACTION,
    }

    db_session_adapter.release_read_transaction(db_session)
    replay = repair_rejected_deposit_intents(db_session, command)
    assert replay.applied_count == 0
    assert replay.already_applied is True


def test_preview_quarantines_amount_mismatch(db_session, subscriber):
    _stale_pair(db_session, subscriber, proof_amount="13000.00")

    preview = preview_rejected_deposit_intent_repairs(db_session)

    assert preview.repairs == ()
    assert preview.candidates[0].classification is (
        RejectedIntentRepairClassification.evidence_mismatch
    )
