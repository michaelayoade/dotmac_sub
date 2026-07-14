"""Reseller consolidated bank-transfer proofs with withholding tax.

A reseller transfers cash net of WHT and uploads the receipt; on verification
the billing account is credited the *gross* and a WithholdingTaxRecord captures
the withheld tax as a receivable.
"""

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import Payment, PaymentStatus
from app.models.payment_proof import (
    WithholdingTaxRecord,
    WithholdingTaxStatus,
    WithholdingTaxTransition,
)
from app.models.subscriber import Reseller
from app.schemas.billing import PaymentSyncRead
from app.services import billing as billing_service
from app.services import payment_proofs as svc


def _reseller_account(db_session):
    reseller = Reseller(name="Acme Reseller", contact_email="ops@acme.example.com")
    db_session.add(reseller)
    db_session.commit()
    ba = billing_service.billing_accounts.get_for_reseller(db_session, str(reseller.id))
    return reseller, ba


def test_submit_consolidated_derives_gross_and_wht_from_rate(db_session):
    _, ba = _reseller_account(db_session)
    # Net 95,000 transferred at 5% WHT -> gross 100,000, wht 5,000.
    out = svc.submit_proof(
        db_session,
        None,
        submitted_by=None,
        amount="95000",
        billing_account_id=str(ba.id),
        wht_rate="5",
        reference="BULK-1",
        file_path="uploads/payment_proofs/bulk1.png",
    )
    assert out["account_id"] is None
    assert out["billing_account_id"] == str(ba.id)
    assert Decimal(str(out["amount"])) == Decimal("95000.00")
    assert Decimal(str(out["gross_amount"])) == Decimal("100000.00")
    assert Decimal(str(out["wht_amount"])) == Decimal("5000.00")


def test_submit_consolidated_with_explicit_gross(db_session):
    _, ba = _reseller_account(db_session)
    out = svc.submit_proof(
        db_session,
        None,
        submitted_by=None,
        amount="95000",
        billing_account_id=str(ba.id),
        gross_amount="100000",
        reference="BULK-2",
        file_path="uploads/payment_proofs/bulk2.png",
    )
    assert Decimal(str(out["wht_amount"])) == Decimal("5000.00")
    # Rate is back-derived from gross/net.
    assert Decimal(str(out["wht_rate"])) == Decimal("5.00")


def test_submit_rejects_gross_below_net(db_session):
    _, ba = _reseller_account(db_session)
    with pytest.raises(HTTPException) as exc:
        svc.submit_proof(
            db_session,
            None,
            submitted_by=None,
            amount="95000",
            billing_account_id=str(ba.id),
            gross_amount="90000",
            file_path="uploads/payment_proofs/bad.png",
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize("rate", ["100", "101", "-1"])
def test_submit_rejects_invalid_wht_rate(db_session, rate):
    _, ba = _reseller_account(db_session)

    with pytest.raises(HTTPException) as exc:
        svc.submit_proof(
            db_session,
            None,
            submitted_by=None,
            amount="95000",
            billing_account_id=str(ba.id),
            wht_rate=rate,
            file_path="uploads/payment_proofs/bad-rate.png",
        )

    assert exc.value.status_code == 400
    assert "WHT rate" in exc.value.detail


def test_submit_rejects_inconsistent_gross_net_and_rate(db_session):
    _, ba = _reseller_account(db_session)

    with pytest.raises(HTTPException) as exc:
        svc.submit_proof(
            db_session,
            None,
            submitted_by=None,
            amount="95000",
            billing_account_id=str(ba.id),
            gross_amount="100000",
            wht_rate="7.5",
            file_path="uploads/payment_proofs/inconsistent.png",
        )

    assert exc.value.status_code == 400
    assert "do not reconcile" in exc.value.detail


def test_verify_consolidated_credits_gross_and_raises_wht_receivable(db_session):
    _, ba = _reseller_account(db_session)
    proof = svc.submit_proof(
        db_session,
        None,
        submitted_by=None,
        amount="95000",
        billing_account_id=str(ba.id),
        wht_rate="5",
        reference="BULK-3",
        file_path="uploads/payment_proofs/bulk3.png",
    )

    out = svc.verify_proof(db_session, proof["id"], verified_by="admin-1")
    assert out["status"] == "verified"
    assert out["withholding_tax_record_id"] is not None

    payment = db_session.get(Payment, out["payment_id"])
    assert payment.status == PaymentStatus.succeeded
    assert payment.billing_account_id == ba.id
    # Account credited the full gross, not just the net cash received.
    assert Decimal(str(payment.amount)) == Decimal("100000.00")

    record = db_session.get(WithholdingTaxRecord, out["withholding_tax_record_id"])
    assert record.status == WithholdingTaxStatus.pending
    assert Decimal(str(record.gross_amount)) == Decimal("100000.00")
    assert Decimal(str(record.net_amount)) == Decimal("95000.00")
    assert Decimal(str(record.wht_amount)) == Decimal("5000.00")
    assert record.billing_account_id == ba.id
    sync_rows = billing_service.payments.sync_list_response(
        db_session,
        account_id=None,
        status=None,
        is_active=None,
        updated_since=None,
        limit=500,
        offset=0,
    )["items"]
    sync_payment = PaymentSyncRead.model_validate(
        next(item for item in sync_rows if item.id == payment.id)
    )
    assert sync_payment.gross_amount == Decimal("100000.00")
    assert sync_payment.net_amount == Decimal("95000.00")
    assert sync_payment.wht_amount == Decimal("5000.00")
    assert sync_payment.wht_rate == Decimal("5.00")
    assert sync_payment.wht_status == WithholdingTaxStatus.pending.value
    assert sync_payment.wht_record_id == record.id
    timeline = (
        db_session.query(WithholdingTaxTransition)
        .filter(WithholdingTaxTransition.record_id == record.id)
        .all()
    )
    assert len(timeline) == 1
    assert timeline[0].from_status is None
    assert timeline[0].to_status == WithholdingTaxStatus.pending

    # Surfaced by the listing helper for reseller/admin WHT views.
    listed = svc.list_withholding_tax_records(db_session, billing_account_id=str(ba.id))
    assert len(listed) == 1
    assert Decimal(str(listed[0]["wht_amount"])) == Decimal("5000.00")


def test_verify_amount_correction_preserves_gross_and_recomputes_wht(db_session):
    _, ba = _reseller_account(db_session)
    proof = svc.submit_proof(
        db_session,
        None,
        submitted_by=None,
        amount="95000",
        billing_account_id=str(ba.id),
        gross_amount="100000",
        wht_rate="5",
        reference="BULK-CORRECTED",
        file_path="uploads/payment_proofs/corrected.png",
    )

    out = svc.verify_proof(
        db_session,
        proof["id"],
        verified_by="admin-1",
        amount="92500",
    )
    payment = db_session.get(Payment, out["payment_id"])
    record = db_session.get(WithholdingTaxRecord, out["withholding_tax_record_id"])

    assert payment.amount == Decimal("100000.00")
    assert record.gross_amount == Decimal("100000.00")
    assert record.net_amount == Decimal("92500.00")
    assert record.wht_amount == Decimal("7500.00")
    assert record.wht_rate == Decimal("7.50")


def test_verify_consolidated_without_wht_credits_net(db_session):
    _, ba = _reseller_account(db_session)
    proof = svc.submit_proof(
        db_session,
        None,
        submitted_by=None,
        amount="50000",
        billing_account_id=str(ba.id),
        reference="BULK-4",
        file_path="uploads/payment_proofs/bulk4.png",
    )
    out = svc.verify_proof(db_session, proof["id"], verified_by="admin-1")
    assert out["withholding_tax_record_id"] is None
    payment = db_session.get(Payment, out["payment_id"])
    assert Decimal(str(payment.amount)) == Decimal("50000.00")
    assert (
        svc.list_withholding_tax_records(db_session, billing_account_id=str(ba.id))
        == []
    )
