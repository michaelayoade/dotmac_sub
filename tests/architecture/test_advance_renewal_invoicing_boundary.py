from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_advance_renewal_has_no_implicit_notice_day() -> None:
    owner = _source("app/services/advance_renewal_invoicing.py")
    settings = _source("app/services/settings_spec.py")

    assert 'key="renewal_invoice_notice_days"' in settings
    assert "default=None" in settings
    assert "raw_days or 7" not in owner
    assert "days_before=7" not in owner


def test_task_delegates_and_owner_controls_dates_and_invoice_writes() -> None:
    task = _source("app/tasks/billing.py")
    scheduled = _source("app/services/billing/scheduled.py")
    owner = _source("app/services/advance_renewal_invoicing.py")

    assert "run_advance_renewal_invoices" in task
    assert "generate_advance_renewal_invoice" in scheduled
    assert "billing_period_start=period_start" in owner
    assert "due_at=period_start" in owner
    assert "subscription.next_billing_at =" not in owner
    assert "Invoices.stage_system_invoice" in owner
    assert "InvoiceLines.stage_system_line" in owner
