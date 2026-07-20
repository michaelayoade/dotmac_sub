from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import HTTPException

from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services import payment_proofs
from app.services import web_billing_payment_proofs as web_payment_proofs
from app.services.action_forms import ActionFieldKind, ActionFormSubmission


def _proof(
    *,
    status: PaymentProofStatus = PaymentProofStatus.submitted,
    consolidated: bool = False,
) -> PaymentProof:
    return PaymentProof(
        id=uuid.uuid4(),
        account_id=None if consolidated else uuid.uuid4(),
        billing_account_id=uuid.uuid4() if consolidated else None,
        amount=Decimal("9000.00") if consolidated else Decimal("5000.00"),
        gross_amount=Decimal("10000.00") if consolidated else None,
        wht_amount=Decimal("1000.00") if consolidated else None,
        wht_rate=Decimal("10.00") if consolidated else None,
        currency="NGN",
        reference="TRF-ACTION",
        file_path="uploads/payment_proofs/action.png",
        status=status,
    )


def test_review_eligibility_is_owned_by_payment_proofs() -> None:
    proof = _proof()
    duplicate = _proof(status=PaymentProofStatus.verified)

    eligible = payment_proofs.review_eligibility(proof)
    duplicate_result = payment_proofs.review_eligibility(proof, [duplicate])
    terminal = payment_proofs.review_eligibility(
        _proof(status=PaymentProofStatus.rejected)
    )

    assert eligible.verify_allowed is True
    assert eligible.reject_allowed is True
    assert duplicate_result.verify_allowed is False
    assert str(duplicate.id) in str(duplicate_result.verify_unavailable_reason)
    assert duplicate_result.reject_allowed is True
    assert terminal.verify_allowed is False
    assert terminal.reject_allowed is False


def test_subscriber_review_actions_declare_fields_impact_and_confirmation() -> None:
    actions = web_payment_proofs._review_actions(
        _proof(), [], can_review=True, submission=None
    )

    verify, reject = actions
    assert verify.key == web_payment_proofs.VERIFY_ACTION_KEY
    assert verify.allowed is True
    assert verify.confirmation is not None
    assert "succeeded payment" in str(verify.impact)
    assert [field.key for field in verify.fields] == [
        "amount",
        "auto_allocate",
        "review_notes",
    ]
    assert verify.field("amount").kind is ActionFieldKind.decimal
    assert reject.key == web_payment_proofs.REJECT_ACTION_KEY
    assert reject.field("review_notes").required is True


def test_consolidated_review_projects_net_cash_and_wht_impact() -> None:
    actions = web_payment_proofs._review_actions(
        _proof(consolidated=True), [], can_review=True, submission=None
    )

    verify = actions[0]
    assert [field.key for field in verify.fields] == ["amount", "review_notes"]
    assert verify.field("amount").label == "Confirmed net cash (NGN)"
    assert "WHT receivable" in str(verify.impact)
    assert "WHT receivable" in str(verify.confirmation.message)


def test_duplicate_disables_verify_with_owner_reason_but_keeps_reject() -> None:
    proof = _proof()
    duplicate = _proof(status=PaymentProofStatus.verified)

    verify, reject = web_payment_proofs._review_actions(
        proof, [duplicate], can_review=True, submission=None
    )

    assert verify.allowed is False
    assert str(duplicate.id) in str(verify.disabled_reason)
    assert reject.allowed is True


def test_unauthorized_or_terminal_review_actions_are_omitted() -> None:
    assert (
        web_payment_proofs._review_actions(
            _proof(), [], can_review=False, submission=None
        )
        == ()
    )
    assert (
        web_payment_proofs._review_actions(
            _proof(status=PaymentProofStatus.verified),
            [],
            can_review=True,
            submission=None,
        )
        == ()
    )


def test_failed_submission_binds_typed_field_error_and_values() -> None:
    error = payment_proofs.PaymentProofReviewError(
        status_code=400,
        detail="Invalid verified amount",
        code="invalid_verified_amount",
        field="amount",
    )
    submission = web_payment_proofs.review_error_submission(
        action_key=web_payment_proofs.VERIFY_ACTION_KEY,
        values={
            "amount": "not-a-number",
            "auto_allocate": "no",
            "review_notes": "bank mismatch",
        },
        error=error,
    )

    verify = web_payment_proofs._review_actions(
        _proof(), [], can_review=True, submission=submission
    )[0]

    assert verify.field("amount").value == "not-a-number"
    assert verify.field("amount").error == "Invalid verified amount"
    assert verify.field("auto_allocate").value == "no"
    assert verify.field("review_notes").value == "bank mismatch"


def test_untyped_command_error_becomes_general_error() -> None:
    submission = web_payment_proofs.review_error_submission(
        action_key=web_payment_proofs.VERIFY_ACTION_KEY,
        values={"amount": "5000.00", "auto_allocate": "yes"},
        error=HTTPException(status_code=409, detail="Reference already verified"),
    )

    verify = web_payment_proofs._review_actions(
        _proof(), [], can_review=True, submission=submission
    )[0]

    assert verify.general_error == "Reference already verified"


def test_consolidated_binding_discards_inapplicable_allocation_value() -> None:
    submission = ActionFormSubmission.from_mapping(
        web_payment_proofs.VERIFY_ACTION_KEY,
        {
            "amount": "8500.00",
            "auto_allocate": "no",
            "review_notes": "confirmed",
        },
    )

    verify = web_payment_proofs._review_actions(
        _proof(consolidated=True), [], can_review=True, submission=submission
    )[0]

    assert verify.field("amount").value == "8500.00"
    assert verify.field("review_notes").value == "confirmed"
