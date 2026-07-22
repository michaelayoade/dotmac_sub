"""Retire mutable customer-financial lifecycle controls.

Revision ID: 398_permanent_financial_lifecycle
Revises: 397_validate_payment_prepaid_archive

Sub owns the live customer-financial lifecycle after cutover. Billing,
collections, renewal, restoration, notification delivery, and their recovery
runners are permanent responsibilities. This migration removes stale switch
rows and re-enables the corresponding scheduled tasks. Per-account approval,
funding, coverage, quarantine, shields, grace, and provider capability facts
remain authoritative inputs.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "398_permanent_financial_lifecycle"
down_revision = "397_validate_payment_prepaid_archive"
branch_labels = None
depends_on = None

_RETIRED_SETTINGS = (
    ("modules", "module_billing_enabled"),
    ("modules", "module_catalog_enabled"),
    ("modules", "module_customer_enabled"),
    ("modules", "module_notifications_enabled"),
    ("modules", "billing_invoicing"),
    ("modules", "billing_autopay"),
    ("modules", "billing_collections"),
    ("modules", "billing_overdue_marking"),
    ("modules", "billing_arrangements"),
    ("modules", "billing_topup_reconciliation"),
    ("modules", "billing_notifications_hourly"),
    ("modules", "collections_prepaid_balance_enforcement"),
    ("modules", "billing_prepaid_service_renewals"),
    ("modules", "billing_direct_bank_transfer"),
    ("modules", "catalog_subscription_expiration"),
    ("modules", "catalog_vacation_hold_resume"),
    ("modules", "customer_services_view"),
    ("modules", "notifications_queue"),
    ("billing", "billing_enabled"),
    ("billing", "billing_enabled_expected"),
    ("billing", "autopay_enabled"),
    ("billing", "overdue_check_enabled"),
    ("billing", "topup_reconciliation_enabled"),
    ("billing", "direct_bank_transfer_enabled"),
    ("billing", "prepaid_service_renewals_enabled"),
    ("billing", "settle_credit_on_invoice_enabled"),
    ("billing", "customer_balance_notifications_enabled"),
    ("billing", "send_billing_notifications"),
    ("billing", "proration_enabled"),
    ("billing", "invoice_number_enabled"),
    ("billing", "credit_note_number_enabled"),
    ("billing", "auto_activate_pending_on_billing"),
    ("billing", "bill_backdated_periods"),
    ("collections", "dunning_enabled"),
    ("collections", "arrangement_check_enabled"),
    ("collections", "billing_notifications_hourly_enabled"),
    ("collections", "prepaid_balance_enforcement_enabled"),
    ("collections", "billing_enforcement_health_gates_enabled"),
    ("collections", "billing_enforcement_require_notification_health"),
    ("collections", "billing_enforcement_require_payment_health"),
    ("collections", "billing_enforcement_settle_credit_before_dunning_enabled"),
    ("collections", "enforcement_window_mode"),
    ("collections", "enforcement_skip_weekends"),
    ("collections", "enforcement_skip_holidays"),
    ("collections", "prepaid_blocking_time"),
    ("collections", "prepaid_skip_weekends"),
    ("collections", "prepaid_skip_holidays"),
    ("collections", "prepaid_readiness_max_age_minutes"),
    ("collections", "prepaid_activation_max_grace_days"),
    ("catalog", "subscription_expiration_enabled"),
    ("catalog", "vacation_hold_auto_resume_enabled"),
    ("catalog", "scheduled_plan_change_enabled"),
    ("catalog", "scheduled_status_change_enabled"),
    ("catalog", "customer_suspend_enabled"),
    ("notification", "notification_queue_enabled"),
    ("notification", "status_gate_enabled"),
    ("notification", "notification_quiet_hours_enabled"),
    ("radius", "enforce_stopped_disabled"),
    ("scheduler", "event_dispatch_enabled"),
    ("scheduler", "event_retry_enabled"),
    ("scheduler", "event_stale_cleanup_enabled"),
)

_PERMANENT_TASK_NAMES = (
    "app.tasks.billing.run_invoice_cycle",
    "app.tasks.billing.mark_invoices_overdue",
    "app.tasks.billing.run_billing_notifications",
    "app.tasks.billing.check_billing_switch",
    "app.tasks.collections.run_billing_enforcement",
    "app.tasks.collections.run_bundle_reconcile",
    "app.tasks.collections.prepaid_balance_sweep",
    "app.tasks.autopay.charge_due_invoices",
    "app.tasks.arrangements.check_overdue_arrangements",
    "app.tasks.payment_reconciliation.reconcile_topups",
    "app.tasks.catalog.expire_subscriptions",
    "app.tasks.catalog.apply_due_subscription_changes",
    "app.tasks.catalog.apply_due_subscription_status_commands",
    "app.tasks.vacation_holds.resume_expired_holds",
    "app.tasks.notifications.deliver_notification_queue",
    "app.tasks.events.dispatch_pending_events",
    "app.tasks.events.retry_failed_events",
    "app.tasks.events.mark_stale_processing_events",
)

_DELETE_SETTING = sa.text(
    "DELETE FROM domain_settings "
    "WHERE domain = CAST(:domain AS settingdomain) AND key = :key"
)


def upgrade() -> None:
    for domain, key in _RETIRED_SETTINGS:
        op.execute(_DELETE_SETTING.bindparams(domain=domain, key=key))
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = CAST('notification' AS settingdomain) "
            "AND key LIKE 'notification_event_%_enabled'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE scheduled_tasks SET enabled = true, updated_at = now() "
            "WHERE task_name IN :task_names"
        ).bindparams(
            sa.bindparam("task_names", expanding=True, value=_PERMANENT_TASK_NAMES)
        )
    )


def downgrade() -> None:
    # Forward-only authority cutover: removed switches must not be recreated.
    pass
