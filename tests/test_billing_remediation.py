"""Tests for the finance remediation executor — refusals weighted as heavily as
happy paths. This tool mutates money records, so the guards matter most.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services import billing_remediation as br


def _account(db):
    s = Subscriber(first_name="R", last_name="M", email=f"{id(object())}@e.com")
    db.add(s)
    db.flush()
    return s


def _invoice(
    db, account, *, total="100.00", balance_due="100.00", status=InvoiceStatus.issued
):
    inv = Invoice(
        account_id=account.id,
        status=status,
        currency="NGN",
        subtotal=Decimal(total),
        total=Decimal(total),
        balance_due=Decimal(balance_due),
        is_active=True,
    )
    db.add(inv)
    db.flush()
    return inv


def _line(db, invoice, amount="100.00"):
    line = InvoiceLine(
        invoice_id=invoice.id, description="x", amount=Decimal(amount), is_active=True
    )
    db.add(line)
    db.flush()
    return line


def _row(line, invoice, action, **overrides):
    row = {
        "invoice_line_id": str(line.id),
        "action": action,
        "line_amount": str(line.amount),
        "invoice_status": invoice.status.value,
        "invoice_balance_due": str(invoice.balance_due),
    }
    row.update(overrides)
    return row


def _plan_one(db, row):
    return br.plan_row(db, row)


class TestRefusals:
    def test_unknown_action(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a)
        ln = _line(db_session, inv)
        db_session.commit()
        assert _plan_one(db_session, _row(ln, inv, "delete_everything"))["reason"] == (
            "unknown_action"
        )

    def test_void_refused_when_paid(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a, total="100.00", balance_due="40.00")  # paid 60
        ln = _line(db_session, inv)
        db_session.commit()
        r = _plan_one(db_session, _row(ln, inv, "void_unpaid_line"))
        assert r["decision"] == "refuse" and r["reason"] == "invoice_paid_use_credit"

    def test_credit_refused_when_unpaid(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a, balance_due="100.00")  # unpaid
        ln = _line(db_session, inv)
        db_session.commit()
        r = _plan_one(db_session, _row(ln, inv, "credit_paid_line"))
        assert r["decision"] == "refuse" and r["reason"] == "invoice_unpaid_use_void"

    def test_line_amount_drift(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a)
        ln = _line(db_session, inv, "100.00")
        db_session.commit()
        row = _row(ln, inv, "void_unpaid_line", line_amount="999.00")
        assert _plan_one(db_session, row)["reason"] == "line_amount_changed"

    def test_invoice_status_drift(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a)
        ln = _line(db_session, inv)
        db_session.commit()
        row = _row(ln, inv, "void_unpaid_line", invoice_status="void")
        assert _plan_one(db_session, row)["reason"] == "invoice_status_changed"

    def test_invoice_balance_drift(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a)
        ln = _line(db_session, inv)
        db_session.commit()
        row = _row(ln, inv, "void_unpaid_line", invoice_balance_due="55.00")
        assert _plan_one(db_session, row)["reason"] == "invoice_balance_changed"

    def test_inactive_line_refused(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a)
        ln = _line(db_session, inv)
        ln.is_active = False
        db_session.commit()
        assert _plan_one(db_session, _row(ln, inv, "void_unpaid_line"))["reason"] == (
            "line_missing_or_inactive"
        )

    def test_bad_line_id_refused(self, db_session):
        row = {
            "invoice_line_id": "not-a-uuid",
            "action": "void_unpaid_line",
            "line_amount": "1",
            "invoice_status": "issued",
            "invoice_balance_due": "1",
        }
        assert _plan_one(db_session, row)["decision"] == "refuse"


class TestApply:
    def test_void_unpaid_line(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a, balance_due="100.00")  # unpaid
        ln = _line(db_session, inv)
        db_session.commit()
        plan = br.plan_remediation(db_session, [_row(ln, inv, "void_unpaid_line")])
        assert plan["counts"]["apply"] == 1
        res = br.apply_remediation(db_session, plan, dry_run=False)
        assert res["applied_count"] == 1
        db_session.refresh(ln)
        assert ln.is_active is False

    def test_dry_run_writes_nothing(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a, balance_due="100.00")
        ln = _line(db_session, inv)
        db_session.commit()
        plan = br.plan_remediation(db_session, [_row(ln, inv, "void_unpaid_line")])
        res = br.apply_remediation(db_session, plan, dry_run=True)
        assert res["applied_count"] == 0
        db_session.refresh(ln)
        assert ln.is_active is True  # untouched

    def test_mark_valid_historical_no_change(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a)
        ln = _line(db_session, inv)
        db_session.commit()
        plan = br.plan_remediation(db_session, [_row(ln, inv, "mark_valid_historical")])
        assert plan["counts"]["skip"] == 1
        res = br.apply_remediation(db_session, plan, dry_run=False)
        assert res["applied_count"] == 0
        db_session.refresh(ln)
        assert ln.is_active is True

    def test_credit_paid_line_creates_credit_note(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a, total="100.00", balance_due="40.00")  # paid 60
        ln = _line(db_session, inv)
        db_session.commit()
        plan = br.plan_remediation(db_session, [_row(ln, inv, "credit_paid_line")])
        assert plan["counts"]["apply"] == 1
        res = br.apply_remediation(db_session, plan, dry_run=False)
        assert res["applied_count"] == 1
        cn_id = res["applied"][0]["after"]["credit_note_id"]
        from app.models.billing import CreditNote

        cn = db_session.get(CreditNote, cn_id)
        assert cn is not None
        assert Decimal(str(cn.total)) == Decimal("100.00")
        # the original paid line is NOT voided (credit note offsets it instead)
        db_session.refresh(ln)
        assert ln.is_active is True

    def test_rollback_voids_credit_note(self, db_session):
        from app.models.billing import CreditNote, CreditNoteStatus

        a = _account(db_session)
        inv = _invoice(db_session, a, total="100.00", balance_due="40.00")
        ln = _line(db_session, inv)
        db_session.commit()
        plan = br.plan_remediation(db_session, [_row(ln, inv, "credit_paid_line")])
        manifest = br.apply_remediation(db_session, plan, dry_run=False)
        cn_id = manifest["applied"][0]["after"]["credit_note_id"]
        br.rollback_remediation(db_session, manifest)
        cn = db_session.get(CreditNote, cn_id)
        assert cn.status == CreditNoteStatus.void

    def test_rollback_reactivates_voided_line(self, db_session):
        a = _account(db_session)
        inv = _invoice(db_session, a, balance_due="100.00")
        ln = _line(db_session, inv)
        db_session.commit()
        plan = br.plan_remediation(db_session, [_row(ln, inv, "void_unpaid_line")])
        manifest = br.apply_remediation(db_session, plan, dry_run=False)
        db_session.refresh(ln)
        assert ln.is_active is False
        br.rollback_remediation(db_session, manifest)
        db_session.refresh(ln)
        assert ln.is_active is True


class TestLoad:
    def test_missing_columns_raises(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("invoice_line_id,action\nx,void_unpaid_line\n")
        try:
            br.load_disposition_csv(str(p))
            raise AssertionError("expected ValueError for missing snapshot columns")
        except ValueError as exc:
            assert "missing required columns" in str(exc)
