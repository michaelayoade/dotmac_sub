"""The payment form cannot become a second withholding acceptance writer."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.billing import LedgerEntry, Payment, PaymentStatus
from app.schemas.billing import (
    ManualPaymentRecordingConfirm,
    ManualPaymentRecordingPreviewRequest,
)
from app.services import manual_payment_recording as owner
from app.services.owner_commands import CommandContext


def _request(subscriber):
    return ManualPaymentRecordingPreviewRequest(
        account_id=subscriber.id,
        amount=Decimal("100"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id="ACTUAL-BANK-REF",
        auto_allocate=False,
        withholding_tax=True,
    )


def test_forged_manual_withholding_refuses_before_payment_preview(
    db_session, subscriber, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported WHT must not reach the cash writer")

    monkeypatch.setattr(owner.Payments, "preview_creation", forbidden)
    with pytest.raises(owner.ManualPaymentRecordingError) as caught:
        owner.preview_manual_payment_recording(db_session, _request(subscriber))
    assert caught.value.code.endswith("withholding_invoice_flow_required")
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_forged_manual_withholding_confirm_cannot_reach_command_writer(
    db_session, subscriber, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unsupported WHT must not enter a financial command")

    monkeypatch.setattr(owner, "execute_owner_command", forbidden)
    request = ManualPaymentRecordingConfirm(
        **_request(subscriber).model_dump(),
        preview_fingerprint="a" * 64,
        control_fingerprint="b" * 64,
        idempotency_key="forged-wht-manual",
    )
    with pytest.raises(owner.ManualPaymentRecordingError) as caught:
        owner.confirm_manual_payment_recording(
            db_session,
            request=request,
            context=CommandContext.system(
                actor="user:test-finance",
                scope=owner.MANUAL_PAYMENT_RECORDING_SCOPE,
                reason="Exercise unsupported secondary withholding path",
            ),
        )
    assert caught.value.code.endswith("withholding_invoice_flow_required")
