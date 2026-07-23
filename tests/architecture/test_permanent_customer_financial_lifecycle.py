"""Customer-financial lifecycle owners are permanent after authority cutover."""

from pathlib import Path

from app.services.control_registry import all_controls
from app.services.scheduler import PERMANENT_CUSTOMER_LIFECYCLE_TASKS

ROOT = Path(__file__).resolve().parents[2]

RETIRED_CONTROL_KEYS = {
    "billing.autopay",
    "billing.collections",
    "billing.invoicing",
    "billing.overdue_marking",
    "billing.prepaid_service_renewals",
    "catalog.subscription_expiration",
    "collections.prepaid_balance_enforcement",
    "notifications.queue",
}

REQUIRED_PERMANENT_TASKS = {
    "app.tasks.billing.run_invoice_cycle",
    "app.tasks.billing.mark_invoices_overdue",
    "app.tasks.collections.run_billing_enforcement",
    "app.tasks.collections.prepaid_balance_sweep",
    "app.tasks.autopay.charge_due_invoices",
    "app.tasks.catalog.expire_subscriptions",
    "app.tasks.enforcement.reconcile_billing_approval_drift",
    "app.tasks.notifications.deliver_notification_queue",
    "app.tasks.events.dispatch_pending_events",
    "app.tasks.radius.run_enforcement_reconciler",
}


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_financial_lifecycle_controls_are_absent_from_registry():
    registered = {control.key for control in all_controls()}
    assert RETIRED_CONTROL_KEYS.isdisjoint(registered)


def test_core_lifecycle_tasks_are_declared_permanent():
    assert REQUIRED_PERMANENT_TASKS <= PERMANENT_CUSTOMER_LIFECYCLE_TASKS


def test_only_shared_enforcement_time_window_remains():
    settings = _read("app/services/settings_spec.py")
    assert 'key="enforcement_window_start"' in settings
    assert 'key="enforcement_window_end"' in settings
    for retired in (
        "enforcement_window_mode",
        "enforcement_skip_weekends",
        "enforcement_skip_holidays",
        "prepaid_blocking_time",
        "prepaid_skip_weekends",
        "prepaid_skip_holidays",
    ):
        assert f'key="{retired}"' not in settings


def test_cutover_migration_removes_controls_and_reenables_tasks():
    migration = _read("alembic/versions/398_permanent_customer_financial_lifecycle.py")
    assert '("billing", "billing_enabled")' in migration
    assert '("collections", "prepaid_balance_enforcement_enabled")' in migration
    assert '("notification", "notification_quiet_hours_enabled")' in migration
    assert "UPDATE scheduled_tasks SET enabled = true" in migration
    assert "Forward-only authority cutover" in migration


def test_account_approval_remains_a_canonical_fact():
    subscriber = _read("app/models/subscriber.py")
    access = _read("app/services/access_resolution.py")
    assert "billing_enabled: Mapped[bool]" in subscriber
    assert "account_billing_enabled" in access


def test_lifecycle_events_cannot_bypass_the_post_commit_outbox():
    lifecycle = _read("app/services/account_lifecycle.py")
    assert "defer_until_commit=False" not in lifecycle
    assert "from app.services.events import emit_event as" not in lifecycle
