"""A reseller bank transfer must be credited once, not twice.

F16. ``verify_proof`` dispatches a reseller (consolidated) proof to
``_verify_consolidated_proof`` BEFORE it takes the account lock and re-checks the
status — the subscriber path does both, this one did neither.

So the status check in front of the dispatch was an UNLOCKED read: two reviewers
clicking Verify at the same time both passed it and both created a succeeded
Payment for the gross value. The reseller's balance was credited twice, with two
WithholdingTaxRecord rows to match.

Nothing else caught it: ``find_duplicate_proofs`` excludes the proof itself, and
``uq_payments_active_external_id`` only fires when ``provider_id IS NOT NULL`` —
a proof-backed payment has none.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import BillingAccount, Payment
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.models.subscriber import Reseller, Subscriber
from app.services import payment_proofs


def _reseller_billing_account(db) -> BillingAccount:
    reseller = Reseller(name=f"R-{uuid.uuid4().hex[:6]}")
    db.add(reseller)
    db.commit()
    db.refresh(reseller)

    ba = BillingAccount(
        reseller_id=reseller.id,
        name=f"BA-{uuid.uuid4().hex[:6]}",
        currency="NGN",
    )
    db.add(ba)
    db.commit()
    db.refresh(ba)
    return ba


def _submitted_proof(db, ba, amount: str) -> PaymentProof:
    proof = PaymentProof(
        billing_account_id=ba.id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentProofStatus.submitted,
        bank_name="Zenith",
        file_path=f"proofs/{uuid.uuid4().hex[:8]}.pdf",
        reference=f"ref-{uuid.uuid4().hex[:8]}",
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    return proof


def _submitted_subscriber_proof(db, amount: str) -> PaymentProof:
    subscriber = Subscriber(
        first_name="Proof",
        last_name="Owner",
        email=f"proof-{uuid.uuid4().hex[:8]}@example.invalid",
    )
    db.add(subscriber)
    db.commit()
    proof = PaymentProof(
        account_id=subscriber.id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentProofStatus.submitted,
        bank_name="Zenith",
        file_path=f"proofs/{uuid.uuid4().hex[:8]}.pdf",
        reference=f"ref-{uuid.uuid4().hex[:8]}",
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    return proof


def _payments_for(db, ba) -> list[Payment]:
    return (
        db.query(Payment)
        .filter(Payment.billing_account_id == ba.id)
        .filter(Payment.is_active.is_(True))
        .all()
    )


def test_verifying_a_reseller_proof_twice_credits_it_once(db_session):
    """The second reviewer must lose the race, not double-credit the reseller."""
    ba = _reseller_billing_account(db_session)
    proof = _submitted_proof(db_session, ba, "500000.00")

    payment_proofs.verify_proof(db_session, str(proof.id), verified_by="reviewer-1")
    db_session.commit()

    assert len(_payments_for(db_session, ba)) == 1

    # The second reviewer's click. Before the fix this passed the unlocked status
    # check and created a SECOND payment for the same bank transfer.
    with pytest.raises(HTTPException) as exc:
        payment_proofs.verify_proof(db_session, str(proof.id), verified_by="reviewer-2")
    assert exc.value.status_code == 400

    payments = _payments_for(db_session, ba)
    assert len(payments) == 1, (
        f"the reseller was credited {len(payments)} times for one bank transfer"
    )


def test_a_verified_reseller_proof_is_terminal(db_session):
    ba = _reseller_billing_account(db_session)
    proof = _submitted_proof(db_session, ba, "100000.00")

    payment_proofs.verify_proof(db_session, str(proof.id), verified_by="reviewer")
    db_session.commit()
    db_session.refresh(proof)

    assert proof.status != PaymentProofStatus.submitted


def test_the_first_verification_still_credits_the_reseller(db_session):
    """The lock must not break the ordinary path."""
    ba = _reseller_billing_account(db_session)
    proof = _submitted_proof(db_session, ba, "250000.00")

    result = payment_proofs.verify_proof(
        db_session, str(proof.id), verified_by="reviewer"
    )
    db_session.commit()

    assert result is not None
    assert len(_payments_for(db_session, ba)) == 1


def test_the_losing_racer_is_refused_under_the_lock(db_session):
    """This is the actual race, and the only test that exercises the fix.

    Two reviewers click Verify at the same time. BOTH pass the unlocked status
    check in verify_proof, because neither has written yet. The second one then
    reaches _verify_consolidated_proof — and that is where it has to be stopped.

    Calling the inner function directly with an already-verified proof reproduces
    exactly the state the loser of the race arrives in. Before the fix it sailed
    through and created a second payment for the same bank transfer.
    """
    ba = _reseller_billing_account(db_session)
    proof = _submitted_proof(db_session, ba, "500000.00")

    # Reviewer 1 wins the race.
    payment_proofs.verify_proof(db_session, str(proof.id), verified_by="reviewer-1")
    db_session.commit()
    assert len(_payments_for(db_session, ba)) == 1

    # Reviewer 2 already passed the unlocked check and is now inside the dispatch.
    with pytest.raises(HTTPException) as exc:
        payment_proofs._verify_consolidated_proof(
            db_session, proof, verified_by="reviewer-2"
        )
    assert exc.value.status_code == 400

    payments = _payments_for(db_session, ba)
    assert len(payments) == 1, (
        f"the reseller was credited {len(payments)} times for one bank transfer"
    )


def test_reseller_payment_stays_in_the_locked_proof_transaction(
    db_session, monkeypatch
):
    """The payment owner must not release the lock before proof verification."""
    from app.services import billing as billing_service

    ba = _reseller_billing_account(db_session)
    proof = _submitted_proof(db_session, ba, "500000.00")
    original_create = billing_service.payments.create
    commit_values = []

    def tracking_create(*args, **kwargs):
        commit_values.append(kwargs.get("commit"))
        return original_create(*args, **kwargs)

    monkeypatch.setattr(billing_service.payments, "create", tracking_create)

    payment_proofs.verify_proof(db_session, str(proof.id), verified_by="reviewer")

    assert commit_values == [False]


def test_subscriber_payment_stays_in_the_locked_proof_transaction(
    db_session, monkeypatch
):
    """The subscriber path has the same lock/commit invariant."""
    from app.services import billing as billing_service

    proof = _submitted_subscriber_proof(db_session, "50000.00")
    original_create = billing_service.payments.create
    commit_values = []

    def tracking_create(*args, **kwargs):
        commit_values.append(kwargs.get("commit"))
        return original_create(*args, **kwargs)

    monkeypatch.setattr(billing_service.payments, "create", tracking_create)

    payment_proofs.verify_proof(db_session, str(proof.id), verified_by="reviewer")

    assert commit_values == [False]
