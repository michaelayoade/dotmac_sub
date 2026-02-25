from __future__ import annotations

from datetime import UTC, datetime

from app.models.billing import BillingRun, BillingRunStatus
from app.models.subscriber import Reseller, Subscriber
from app.services.web_billing_invoice_batch import (
    get_run_row,
    list_recent_runs,
    preview_batch,
    preview_error_payload,
    render_runs_csv,
    render_single_run_csv,
    retry_batch_run,
    run_batch_with_date,
)


def test_list_recent_runs_returns_structured_rows(db_session):
    run = BillingRun(
        run_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        billing_cycle="monthly",
        status=BillingRunStatus.success,
        started_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 2, 1, 10, 1, tzinfo=UTC),
        subscriptions_scanned=50,
        subscriptions_billed=42,
        invoices_created=40,
        lines_created=42,
        skipped=8,
    )
    db_session.add(run)
    db_session.commit()

    rows = list_recent_runs(db_session, limit=10)

    assert len(rows) >= 1
    first = rows[0]
    assert first["billing_cycle"] == "monthly"
    assert first["status"] == "Success"
    assert first["status_badge"] == "success"
    assert first["status_message"] == "Transactions have been created"
    assert first["invoices_created"] == 40
    assert first["duration_seconds"] == 60


def test_preview_error_payload_includes_safe_defaults():
    payload = preview_error_payload(ValueError("boom"))
    assert payload["error"] == "boom"
    assert payload["invoice_count"] == 0
    assert payload["subscriptions"] == []


def test_retry_batch_run_reuses_previous_cycle(db_session, monkeypatch):
    run = BillingRun(
        run_at=datetime(2026, 2, 2, 10, 0, tzinfo=UTC),
        billing_cycle="monthly",
        status=BillingRunStatus.failed,
        started_at=datetime(2026, 2, 2, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 2, 2, 10, 0, tzinfo=UTC),
    )
    db_session.add(run)
    db_session.commit()

    captured = {}

    def _fake_run_batch(db, *, billing_cycle, parse_cycle_fn):
        captured["billing_cycle"] = billing_cycle
        return "ok"

    monkeypatch.setattr("app.services.web_billing_invoice_batch.run_batch", _fake_run_batch)
    note = retry_batch_run(db_session, run_id=str(run.id), parse_cycle_fn=lambda _: None)

    assert note == "ok"
    assert captured["billing_cycle"] == "monthly"


def test_render_runs_csv_contains_headers_and_rows():
    rows = [
        {
            "id": "run-1",
            "run_at": datetime(2026, 2, 1, 8, 0, tzinfo=UTC),
            "created_at": datetime(2026, 2, 1, 8, 0, tzinfo=UTC),
            "billing_cycle": "monthly",
            "subscriptions_scanned": 10,
            "subscriptions_billed": 8,
            "invoices_created": 8,
            "lines_created": 8,
            "skipped": 2,
            "status": "Success",
            "duration_seconds": 30,
            "error": "",
        }
    ]
    csv_text = render_runs_csv(rows)

    assert "run_id,run_at,created_at,billing_cycle" in csv_text
    assert "run-1" in csv_text
    assert "monthly" in csv_text


def test_get_run_row_and_render_single_run_csv(db_session):
    run = BillingRun(
        run_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        billing_cycle="monthly",
        status=BillingRunStatus.success,
        started_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 3, 1, 8, 1, tzinfo=UTC),
        subscriptions_scanned=12,
        subscriptions_billed=9,
        invoices_created=9,
        lines_created=9,
        skipped=3,
    )
    db_session.add(run)
    db_session.commit()

    row = get_run_row(db_session, run_id=str(run.id))

    assert row is not None
    assert row["id"] == str(run.id)
    assert row["billing_cycle"] == "monthly"

    csv_text = render_single_run_csv(row)
    assert "run_id,run_at,created_at,billing_cycle" in csv_text
    assert str(run.id) in csv_text


def test_run_batch_with_date_passes_run_at(monkeypatch):
    captured = {}

    def _fake_run_invoice_cycle(db, billing_cycle, dry_run, run_at):
        captured["run_at"] = run_at
        return {
            "run_at": run_at,
            "invoices_created": 3,
            "subscriptions_billed": 4,
            "skipped": 1,
        }

    monkeypatch.setattr(
        "app.services.web_billing_invoice_batch.billing_automation_service.run_invoice_cycle",
        _fake_run_invoice_cycle,
    )

    note = run_batch_with_date(
        db=None,
        billing_cycle="monthly",
        billing_date="2026-02-24",
        parse_cycle_fn=lambda value: value,
    )

    assert captured["run_at"] is not None
    assert "2026-02-24" in note
    assert "Invoices created: 3" in note


def test_preview_batch_can_group_by_partner(db_session, monkeypatch):
    reseller = Reseller(name="Partner X")
    direct = Subscriber(first_name="Direct", last_name="User", email="direct@example.com")
    partner_user = Subscriber(
        first_name="Partner",
        last_name="User",
        email="partner@example.com",
        reseller=reseller,
    )
    db_session.add_all([reseller, direct, partner_user])
    db_session.commit()

    def _fake_run_invoice_cycle(db, billing_cycle, dry_run, run_at):
        return {
            "invoices_created": 2,
            "accounts_affected": 2,
            "total_amount": 120,
            "subscriptions": [
                {"id": "sub-1", "account_id": str(direct.id), "offer_name": "Direct Plan", "amount": 20},
                {"id": "sub-2", "account_id": str(partner_user.id), "offer_name": "Partner Plan", "amount": 100},
            ],
        }

    monkeypatch.setattr(
        "app.services.web_billing_invoice_batch.billing_automation_service.run_invoice_cycle",
        _fake_run_invoice_cycle,
    )

    payload = preview_batch(
        db_session,
        billing_cycle=None,
        billing_date="2026-02-24",
        separate_by_partner=True,
        parse_cycle_fn=lambda value: value,
    )

    assert len(payload["partner_preview"]) == 2
    assert payload["partner_preview"][0]["partner_name"] in {"Partner X", "Direct"}
