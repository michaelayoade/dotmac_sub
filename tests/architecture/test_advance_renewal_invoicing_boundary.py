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


def test_durable_timer_drives_owner_dates_and_invoice_writes() -> None:
    owner = _source("app/services/advance_renewal_invoicing.py")

    assert "ScheduleTimerCommand" in owner
    assert "schedule_advance_renewal_timer" in owner
    assert "find_due_subscription_ids" not in owner
    assert "billing_period_start=period_start" in owner
    assert "due_at=period_start" in owner
    assert "subscription.next_billing_at =" not in owner
    assert "Invoices.stage_system_invoice" in owner
    assert "InvoiceLines.stage_system_line" in owner


def test_manual_next_period_flow_reuses_owner_and_retires_legacy_admin_writer() -> None:
    owner = _source("app/services/advance_renewal_invoicing.py")
    route = _source("app/web/admin/billing_invoices.py")
    legacy_adapter = _source("app/services/web_billing_invoices.py")
    customer_template = _source("templates/admin/customers/detail.html")

    assert 'concern="reviewed next-period subscription service invoice"' in owner
    assert "execute_owner_command(" in owner
    assert "expected_preview_fingerprint" in owner
    assert "MANUAL_SUBSCRIPTION_SERVICE_INVOICE_SCOPE" in owner
    assert "billing_period_start=charge.period_start" in owner
    assert '"subscription_id": str(subscription.id)' in owner
    assert "subscription.next_billing_at =" not in owner
    assert "/invoices/generate-from-subscription" not in route
    assert "generate_invoice_from_subscription_web" not in legacy_adapter
    assert "/invoices/subscription-service?account_id=" in customer_template


def test_manual_permission_is_assignable_but_not_seeded_to_roles() -> None:
    migration = _source(
        "alembic/versions/689_forward_subscription_service_invoice_permission.py"
    )
    seed = _source("scripts/seed/seed_rbac.py")

    permission = "billing:invoice:create_subscription_service"
    assert permission in migration
    assert "is_ui_assignable" in migration
    assert "INSERT INTO role_permissions" not in migration
    assert "Refusing to remove" in migration
    assert permission in seed
