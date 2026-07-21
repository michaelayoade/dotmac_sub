from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_retired_dunning_task_alias_stays_removed() -> None:
    tasks = _read("app/tasks/collections.py")
    routes = _read("app/celery_app.py")

    assert "app.tasks.collections.run_dunning" not in tasks
    assert "app.tasks.collections.run_dunning" not in routes


def test_retired_prepaid_control_alias_stays_removed() -> None:
    registry = _read("app/services/control_registry.py")
    settings = _read("app/services/settings_spec.py")

    assert '"prepaid_balance_enforcement_enabled"' not in registry
    assert 'key="prepaid_balance_enforcement_enabled"' not in settings


def test_retired_prepaid_monthly_invoice_owner_stays_removed() -> None:
    registry = _read("app/services/control_registry.py")
    settings = _read("app/services/settings_spec.py")
    automation = _read("app/services/billing_automation.py")

    assert 'key="billing.prepaid_monthly_invoicing"' not in registry
    assert 'key="prepaid_monthly_invoicing_enabled"' not in settings
    assert "prepaid_monthly_invoicing_requested" not in automation
    assert "prepaid_runner_draft_until_funded" not in automation
    assert "include_prepaid_monthly" not in automation

    migration = _read("alembic/versions/392_retire_prepaid_monthly_invoice_owner.py")
    assert 'down_revision = "391_payment_receipt_notification"' in migration
    assert 'key="billing_prepaid_monthly_invoicing"' in migration
    assert 'key="prepaid_monthly_invoicing_enabled"' in migration
    assert "downgrade must not recreate" in migration


def test_retired_prepaid_activation_setting_stays_removed() -> None:
    planner = _read("app/services/prepaid_enforcement_planner.py")
    settings = _read("app/services/settings_spec.py")
    seed = _read("app/services/settings_seed.py")

    assert "prepaid_enforcement_activation_at" not in planner
    assert 'key="prepaid_enforcement_activation_at"' not in settings
    assert 'key="prepaid_enforcement_activation_at"' not in seed

    migration = _read("alembic/versions/393_prepaid_coverage_reconciliation.py")
    assert "prepaid_enforcement_activation_at" in migration
    assert "DELETE FROM domain_settings" in migration


def test_retired_prepaid_payment_application_runtime_stays_removed() -> None:
    models = _read("app/models/billing.py")
    payments = _read("app/services/billing/payments.py")
    handler = _read("app/services/events/handlers/prepaid_renewal.py")

    assert "class PaymentPrepaidApplication" not in models
    assert "apply_prepaid_service_credit" not in payments
    assert "prepaid_legacy_cycle_repair" not in payments
    assert "_auto_allocate" not in payments
    assert "EventType.payment_received" in handler

    retirement = _read("alembic/versions/394_retire_payment_prepaid_applications.py")
    assert 'down_revision = "393_prepaid_coverage_reconciliation"' in retirement
    assert "op.rename_table(_LEGACY_TABLE, _ARCHIVE_TABLE)" in retirement
    assert '_ARCHIVE_TABLE = "payment_prepaid_applications_archive"' in retirement
    assert "op.drop_table" not in retirement
    assert "both " in retirement and " exist" in retirement


def test_superseded_prepaid_gap_tools_stay_removed() -> None:
    assert not (ROOT / "scripts/billing/repair_prepaid_legacy_cycle.py").exists()
    assert not (
        ROOT / "scripts/one_off/reconcile_prepaid_service_cycle_gaps.py"
    ).exists()
    acceptance = _read("scripts/one_off/verify_prepaid_deployment_acceptance.py")
    assert "preview_prepaid_coverage_reconciliation" in acceptance
    assert "reconcile_prepaid_service_cycle_gaps" not in acceptance
