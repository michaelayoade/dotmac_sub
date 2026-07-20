"""Evidence-backed, append-only reversal of imported payment batches."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Payment,
    PaymentStatus,
)
from app.models.imports import PaymentImportBatchReversal
from app.models.subscriber import Subscriber
from app.services import import_runs
from app.services.billing._common import get_account_credit_balance
from app.services.financial_import_batch_reversals import (
    PaymentImportBatchReversals,
)
from app.services.web_system_import_wizard import PaymentImportRow, _persist_row
from app.web.admin import system as admin_system


def _account(db) -> Subscriber:
    account = Subscriber(
        first_name="Import",
        last_name="Reversal",
        email=f"import-{uuid.uuid4().hex[:10]}@example.com",
        status="active",
        is_active=True,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _invoice(db, account: Subscriber, amount: str) -> Invoice:
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"IMP-{uuid.uuid4().hex[:8]}",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal(amount),
        tax_total=Decimal("0.00"),
        total=Decimal(amount),
        balance_due=Decimal(amount),
        is_active=True,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def _apply_payment_run(
    db,
    account: Subscriber,
    rows: list[tuple[str, str, str | None]],
):
    lines = ["account_id,amount,status,external_id,invoice_number"]
    lines.extend(
        f"{account.id},{amount},succeeded,{external_id},{invoice_number or ''}"
        for external_id, amount, invoice_number in rows
    )
    csv = "\n".join(lines) + "\n"
    dry = import_runs.create_import_run(
        db,
        module="payments",
        raw_text=csv,
        source_name="payments.csv",
        dry_run=True,
        created_by="operator@example.com",
    )
    dry = import_runs.process_import_run(db, dry.id)
    applied = import_runs.apply_from_dry_run(
        db, dry.id, created_by="operator@example.com"
    )
    assert applied.failed_rows == 0
    return applied


def test_confirm_links_import_settlement_reversal_and_exact_ledger(db_session):
    account = _account(db_session)
    run = _apply_payment_run(
        db_session,
        account,
        [(f"bank-{uuid.uuid4()}", "5000.00", None)],
    )
    row = run.rows[0]
    payment = db_session.get(Payment, row.payment_id)
    assert payment is not None
    assert row.record_created is True
    assert payment.import_run_id == run.id
    assert payment.settlement is not None

    preview = PaymentImportBatchReversals.preview(
        db_session,
        run.id,
        reason="Incorrect bank import",
    )
    assert preview.items[0].payment_settlement_id == payment.settlement.id
    assert preview.items[0].ledger_amount == Decimal("5000.00")
    assert preview.account_positions[0]["unallocated_account_credit_before"] == (
        "5000.00"
    )
    assert preview.account_positions[0]["postpaid_receivables_before"] == "0.00"
    assert preview.access_consequence == ("recheck_each_account_after_payment_reversal")

    result = PaymentImportBatchReversals.confirm(
        db_session,
        run.id,
        reason=preview.reason,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=f"import-reversal-{run.id}",
        actor_id="operator@example.com",
    )

    db_session.refresh(payment)
    batch = result.batch_reversal
    assert payment.status == PaymentStatus.reversed
    assert batch.preview_fingerprint == preview.fingerprint
    assert batch.reversed_payment_count == 1
    assert len(batch.items) == 1
    item = batch.items[0]
    assert item.import_run_row_id == row.id
    assert item.payment_id == payment.id
    assert item.payment_settlement_id == payment.settlement.id
    assert item.payment_reversal_id == payment.reversal.id
    assert item.ledger_entry_id == payment.reversal.ledger_entry_id
    assert item.result_snapshot["ledger_amount"] == "5000.00"
    assert get_account_credit_balance(db_session, str(account.id)) == Decimal("0.00")
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "confirm_payment_import_batch_reversal")
        .filter(AuditEvent.entity_id == str(run.id))
        .one()
    )
    assert audit.actor_id == "operator@example.com"
    assert audit.metadata_["ledger_entry_ids"] == [str(item.ledger_entry_id)]


def test_confirmation_reopens_exact_invoice_receivable(db_session):
    account = _account(db_session)
    invoice = _invoice(db_session, account, "7500.00")
    run = _apply_payment_run(
        db_session,
        account,
        [(f"bank-{uuid.uuid4()}", "7500.00", invoice.invoice_number)],
    )
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")

    preview = PaymentImportBatchReversals.preview(
        db_session, run.id, reason="Wrong invoice payment file"
    )
    assert preview.invoice_effects == (
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "currency": "NGN",
            "receivable_before": "0.00",
            "reopened_amount": "7500.00",
            "receivable_after": "7500.00",
        },
    )

    result = PaymentImportBatchReversals.confirm(
        db_session,
        run.id,
        reason=preview.reason,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=f"import-reversal-{run.id}",
    )
    db_session.refresh(invoice)

    assert invoice.balance_due == Decimal("7500.00")
    assert invoice.status == InvoiceStatus.issued
    source_allocations = result.batch_reversal.items[0].source_snapshot["allocations"]
    assert len(source_allocations) == 1
    assert source_allocations[0]["invoice_id"] == str(invoice.id)
    assert source_allocations[0]["ledger_entry_id"]


def test_multi_payment_batch_reopens_shared_invoice_atomically(db_session):
    account = _account(db_session)
    invoice = _invoice(db_session, account, "300.00")
    run = _apply_payment_run(
        db_session,
        account,
        [
            (f"bank-{uuid.uuid4()}", "100.00", invoice.invoice_number),
            (f"bank-{uuid.uuid4()}", "200.00", invoice.invoice_number),
        ],
    )
    preview = PaymentImportBatchReversals.preview(
        db_session, run.id, reason="Wrong two-row payment file"
    )

    assert len(preview.items) == 2
    assert preview.totals_by_currency[0]["reversal_amount"] == "300.00"
    assert preview.invoice_effects[0]["receivable_after"] == "300.00"
    result = PaymentImportBatchReversals.confirm(
        db_session,
        run.id,
        reason=preview.reason,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=f"import-reversal-{run.id}",
    )
    db_session.refresh(invoice)

    assert invoice.balance_due == Decimal("300.00")
    assert result.batch_reversal.reversed_payment_count == 2
    assert len(result.batch_reversal.items) == 2
    assert {db_session.get(Payment, row.payment_id).status for row in run.rows} == {
        PaymentStatus.reversed
    }


def test_stale_batch_preview_rejects_without_reversing_any_payment(db_session):
    account = _account(db_session)
    run = _apply_payment_run(
        db_session,
        account,
        [(f"bank-{uuid.uuid4()}", "1000.00", None)],
    )
    imported = db_session.get(Payment, run.rows[0].payment_id)
    preview = PaymentImportBatchReversals.preview(
        db_session, run.id, reason="Incorrect bank import"
    )

    _persist_row(
        db_session,
        "payments",
        PaymentImportRow(
            account_id=account.id,
            amount=Decimal("250.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            external_id=f"later-{uuid.uuid4()}",
        ),
        source_name="later.csv",
    )
    db_session.commit()

    with pytest.raises(HTTPException, match="Financial state changed") as stale:
        PaymentImportBatchReversals.confirm(
            db_session,
            run.id,
            reason=preview.reason,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=f"import-reversal-{run.id}",
        )
    assert stale.value.status_code == 409
    db_session.refresh(imported)
    assert imported.status == PaymentStatus.succeeded
    assert db_session.query(PaymentImportBatchReversal).count() == 0


def test_confirmation_replay_returns_same_batch_and_ledger(db_session):
    account = _account(db_session)
    run = _apply_payment_run(
        db_session,
        account,
        [(f"bank-{uuid.uuid4()}", "1250.00", None)],
    )
    preview = PaymentImportBatchReversals.preview(
        db_session, run.id, reason="Duplicate import file"
    )
    kwargs = {
        "reason": preview.reason,
        "preview_fingerprint": preview.fingerprint,
        "idempotency_key": f"import-reversal-{run.id}",
    }
    first = PaymentImportBatchReversals.confirm(db_session, run.id, **kwargs)
    ledger_count = db_session.query(LedgerEntry).count()
    replay = PaymentImportBatchReversals.confirm(db_session, run.id, **kwargs)

    assert replay.idempotent_replay is True
    assert replay.batch_reversal.id == first.batch_reversal.id
    assert db_session.query(PaymentImportBatchReversal).count() == 1
    assert db_session.query(LedgerEntry).count() == ledger_count


def test_mixed_batch_reverses_created_payment_and_skips_reused_payment(db_session):
    account = _account(db_session)
    existing_external_id = f"bank-{uuid.uuid4()}"
    original_run = _apply_payment_run(
        db_session,
        account,
        [(existing_external_id, "100.00", None)],
    )
    existing = db_session.get(Payment, original_run.rows[0].payment_id)
    new_external_id = f"bank-{uuid.uuid4()}"
    mixed_run = _apply_payment_run(
        db_session,
        account,
        [
            (existing_external_id, "100.00", None),
            (new_external_id, "200.00", None),
        ],
    )
    reused_row, created_row = sorted(mixed_run.rows, key=lambda row: row.row_number)
    assert reused_row.record_created is False
    assert reused_row.payment_id == existing.id
    assert created_row.record_created is True

    preview = PaymentImportBatchReversals.preview(
        db_session, mixed_run.id, reason="Mixed duplicate import"
    )
    assert preview.skipped_reused_count == 1
    assert [item.payment_id for item in preview.items] == [created_row.payment_id]
    PaymentImportBatchReversals.confirm(
        db_session,
        mixed_run.id,
        reason=preview.reason,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=f"import-reversal-{mixed_run.id}",
    )

    db_session.refresh(existing)
    created = db_session.get(Payment, created_row.payment_id)
    assert existing.status == PaymentStatus.succeeded
    assert created.status == PaymentStatus.reversed


def test_historical_row_without_creation_provenance_fails_closed(db_session):
    account = _account(db_session)
    run = _apply_payment_run(
        db_session,
        account,
        [(f"bank-{uuid.uuid4()}", "300.00", None)],
    )
    row = run.rows[0]
    payment = db_session.get(Payment, row.payment_id)
    row.record_created = None
    row.payment_id = None
    payment.import_run_id = None
    db_session.commit()

    with pytest.raises(HTTPException, match="lacks durable") as blocked:
        PaymentImportBatchReversals.preview(
            db_session, run.id, reason="Historical rollback"
        )
    assert blocked.value.status_code == 409
    assert payment.status == PaymentStatus.succeeded


def test_admin_adapter_previews_then_confirms_owner_evidence(db_session, monkeypatch):
    import app.web.admin as admin_web

    account = _account(db_session)
    run = _apply_payment_run(
        db_session,
        account,
        [(f"bank-{uuid.uuid4()}", "450.00", None)],
    )
    request = SimpleNamespace()
    monkeypatch.setattr(
        admin_web,
        "get_current_user",
        lambda _request: {"email": "operator@example.com"},
    )
    monkeypatch.setattr(admin_web, "get_sidebar_stats", lambda _db: {})
    monkeypatch.setattr(
        admin_system,
        "parse_form_data_sync",
        lambda _request: {"reason": "Incorrect imported bank file"},
    )
    rendered: dict[str, object] = {}

    def _render(name, context):
        rendered.update({"name": name, "context": context})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(admin_system.templates, "TemplateResponse", _render)
    response = admin_system.system_import_payment_reversal_preview(
        request, str(run.id), db_session
    )

    assert response.status_code == 200
    assert rendered["name"] == (
        "admin/system/import_payment_batch_reversal_confirm.html"
    )
    context = rendered["context"]
    preview = context["preview"]
    monkeypatch.setattr(
        admin_system,
        "parse_form_data_sync",
        lambda _request: {
            "reason": preview.reason,
            "preview_fingerprint": context["preview_fingerprint"],
            "idempotency_key": context["idempotency_key"],
        },
    )
    confirmed = admin_system.system_import_payment_reversal_confirm(
        request, str(run.id), db_session
    )

    assert confirmed.status_code == 303
    assert "Reversed+1+imported+payments" in confirmed.headers["location"]
    batch = db_session.query(PaymentImportBatchReversal).one()
    assert batch.confirmed_by == "operator@example.com"
