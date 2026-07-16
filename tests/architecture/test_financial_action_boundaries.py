"""Financial actions render owner previews; templates do not decide money."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_invoice_template_does_not_derive_receivable_or_credit_eligibility() -> None:
    template = _source("templates/admin/billing/invoice_detail.html")

    forbidden = (
        "totals.paid = totals.paid +",
        "totals.credit = totals.credit +",
        "note.total or 0) - (note.applied_total",
        "if available > 0",
    )
    assert not [pattern for pattern in forbidden if pattern in template]
    assert "invoice_financial_summary.receivable_balance" in template
    assert "credit_application_options" in template


def test_credit_application_adapter_requires_preview_confirmation_evidence() -> None:
    route = _source("app/web/admin/billing_invoice_actions.py")
    confirmation = _source("templates/admin/billing/credit_apply_confirm.html")

    assert '"/invoices/{invoice_id:uuid}/apply-credit/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact ledger transaction" in confirmation


def test_credit_issue_adapter_requires_owner_preview_and_confirmation() -> None:
    route = _source("app/web/admin/billing_credits.py")
    form = _source("templates/admin/billing/credit_form.html")
    confirmation = _source("templates/admin/billing/credit_issue_confirm.html")

    assert '"/credits/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert "confirm('Issue this credit" not in form
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact ledger result" in confirmation
    assert "no direct change" in confirmation


def test_account_adjustments_and_addons_require_owner_confirmation_evidence() -> None:
    api = _source("app/api/billing.py")
    addon_service = _source("app/services/customer_portal_flow_addons.py")
    addon_schema = _source("app/schemas/catalog.py")
    mobile_repository = _source("mobile/lib/src/repositories/catalog_repository.dart")
    data_bundle_screen = _source(
        "mobile/lib/src/features/service/data_bundle_screen.dart"
    )
    plan_change_model = _source("mobile/lib/src/models/plan_change.dart")
    plan_change_template = _source("templates/customer/services/change_plan.html")

    assert '"/account-adjustments/preview"' in api
    assert '"/account-adjustments"' in api
    assert '"/account-adjustments/{adjustment_id}/reversal/preview"' in api
    assert "Direct ledger posting is disabled" in api
    assert "Direct ledger reversal is disabled" in api
    assert "LedgerEntry(" not in addon_service
    assert "AccountAdjustments.confirm(" in addon_service
    assert "sub_add_on.account_adjustment_id" in addon_service
    assert "purchase_preview_fingerprint" in addon_service
    assert "preview_fingerprint: str" in addon_schema
    assert "'preview_fingerprint': previewFingerprint" in mobile_repository
    assert ".addonQuote(" in data_bundle_screen
    assert "quote.previewFingerprint" in data_bundle_screen
    assert "wallet_balance" not in addon_schema
    assert "wallet_balance" not in plan_change_model
    assert "current_balance" not in plan_change_model
    assert "Wallet Balance" not in plan_change_template
    assert "Prepaid Funding" in plan_change_template
    assert "Postpaid Receivables" in plan_change_template


def test_plan_changes_bind_human_preview_to_exact_request_evidence() -> None:
    owner = _source("app/services/prepaid_plan_changes.py")
    request_model = _source("app/models/subscription_change.py")
    customer_route = _source("app/web/customer/routes.py")
    customer_template = _source("templates/customer/services/change_plan.html")
    customer_api_schema = _source("app/schemas/catalog.py")
    mobile_repository = _source("mobile/lib/src/repositories/catalog_repository.dart")
    admin_template = _source("templates/admin/catalog/subscription_detail.html")
    admin_bulk_template = _source("templates/admin/catalog/subscriptions.html")
    admin_edit_template = _source("templates/admin/catalog/subscription_form.html")
    history_template = _source("templates/customer/services/change_requests.html")

    assert '"preview_fingerprint": self.fingerprint' in owner
    assert "expected_preview_fingerprint" in owner
    assert "PrepaidPlanChangePreviewStale" in owner
    assert "confirmation_preview_fingerprint" in request_model
    assert "confirmation_idempotency_key" in request_model
    assert "account_adjustment_id" in request_model
    assert "credit_note_id" in request_model
    assert "ledger_entry_id" in request_model
    assert "preview_fingerprint: str = Form(...)" in customer_route
    assert "idempotency_key: str = Form(...)" in customer_route
    assert 'name="preview_fingerprint"' in customer_template
    assert 'name="idempotency_key"' in customer_template
    assert "Exact ledger result" in customer_template
    assert "preview_fingerprint: str" in customer_api_schema
    assert "idempotency_key: str" in customer_api_schema
    assert "'preview_fingerprint': previewFingerprint" in mobile_repository
    assert "Exact ledger result" in admin_template
    assert (
        "body.set('preview_fingerprint', this.billingQuote().preview_fingerprint)"
        in admin_template
    )
    assert "kind === 'change_plan' ? 'next_cycle' : 'immediate'" in admin_bulk_template
    assert ":disabled=\"kind === 'change_plan'\"" in admin_bulk_template
    assert "Use <strong>Change Plan</strong>" in admin_edit_template
    assert "Exact Result" in history_template
    assert "req.ledger_entry_id" in history_template


def test_payment_refund_adapters_require_owner_preview_and_exact_evidence() -> None:
    route = _source("app/web/admin/billing_payments.py")
    detail = _source("templates/admin/billing/payment_detail.html")
    edit = _source("templates/admin/billing/payment_form.html")
    confirmation = _source("templates/admin/billing/payment_refund_confirm.html")
    provider = _source("app/services/billing/providers.py")

    assert '"/payments/{payment_id:uuid}/refund/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert "status_val == 'succeeded'" not in detail
    assert "edit_capability.allowed" in detail
    assert "refund_capability.allowed" in detail
    assert "onsubmit=\"return confirm('Refund" not in detail
    assert '<option value="refunded"' not in edit
    assert "managed by exact owner evidence" in edit
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact evidence and access consequence" in confirmation
    assert "does not promise a particular service-access state" in confirmation
    assert "Refunds.process_provider_event_refund" in provider
    assert "Payments.mark_status(" in provider
    assert "origin=PaymentSettlementOrigin.provider_event" in provider


def test_payment_reversal_adapters_require_owner_preview_and_exact_evidence() -> None:
    route = _source("app/web/admin/billing_payments.py")
    detail = _source("templates/admin/billing/payment_detail.html")
    edit = _source("templates/admin/billing/payment_form.html")
    confirmation = _source("templates/admin/billing/payment_reversal_confirm.html")
    provider = _source("app/services/billing/providers.py")
    verified_webhook = _source("app/services/api_billing_webhooks.py")
    generic_api = _source("app/api/billing.py")

    assert '"/payments/{payment_id:uuid}/reversal/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert "reversal_capability.allowed" in detail
    assert "edit_capability.allowed" in detail
    assert "PaymentStatus.reversed" not in detail
    assert '<option value="reversed"' not in edit
    assert "managed by exact owner evidence" in edit
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact evidence and access consequence" in confirmation
    assert "does not contact a bank or provider" in confirmation
    assert "PaymentReversals.process_provider_event_reversal" in provider
    assert "trusted_financial_effects=True" in verified_webhook
    assert "trusted_financial_effects=True" not in generic_api


def test_imported_payment_batch_reversal_requires_provenance_and_confirmation() -> None:
    owner = _source("app/services/financial_import_batch_reversals.py")
    import_runs = _source("app/services/import_runs.py")
    legacy_rollback = _source("app/services/web_system_import_wizard.py")
    route = _source("app/web/admin/system.py")
    detail = _source("templates/admin/system/import_run_detail.html")
    confirmation = _source(
        "templates/admin/system/import_payment_batch_reversal_confirm.html"
    )

    assert "record_created = persisted.created_new" in import_runs
    assert "obj.import_run_id = run.id" in import_runs
    assert "row.record_created is None or row.payment_id is None" in owner
    assert "payment.import_run_id != run.id" in owner
    assert "PaymentReversals.process_with_evidence(" in owner
    assert "db.delete(" not in owner
    assert "Legacy financial and subscription import history cannot be raw-deleted" in (
        legacy_rollback
    )
    assert '"/import-runs/{run_id}/payment-reversal-preview"' in route
    assert '"/import-runs/{run_id}/payment-reversal-confirm"' in route
    assert "batch_reversal_capability.allowed" in detail
    assert 'name="reason"' in detail
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact ledger result" in confirmation
    assert "Prepaid funding" in confirmation
    assert "Unallocated credit" in confirmation
    assert "Postpaid receivables" in confirmation
    assert "no restoration or suspension is predicted" in confirmation


def test_payment_creation_settlement_and_allocation_use_owner_confirmation() -> None:
    route = _source("app/web/admin/billing_payments.py")
    api = _source("app/api/billing.py")
    form = _source("templates/admin/billing/payment_form.html")
    creation = _source("templates/admin/billing/payment_create_confirm.html")
    settlement = _source("templates/admin/billing/payment_settlement_confirm.html")
    allocation = _source("templates/admin/billing/payment_allocation_confirm.html")

    assert '"/payments/create/preview"' in route
    assert '"/payments/{payment_id:uuid}/settlement/preview"' in route
    assert '"/payments/{payment_id:uuid}/allocation/preview"' in route
    assert '"/payments/creation/preview"' in api
    assert '"/payment-allocations/preview"' in api
    assert "Record this payment" not in form
    assert 'name="idempotency_token"' in creation
    for confirmation in (creation, settlement, allocation):
        assert 'name="preview_fingerprint"' in confirmation
    for confirmation in (settlement, allocation):
        assert 'name="idempotency_key"' in confirmation
    assert "Prepaid funding position" in creation
    assert "Unallocated account credit" in creation
    assert "Receivable" in allocation
    assert "Exact account-credit ledger" in allocation


def test_invoice_void_and_writeoff_use_owner_preview_and_exact_evidence() -> None:
    route = _source("app/web/admin/billing_invoice_actions.py")
    bulk_route = _source("app/web/admin/billing_invoice_bulk.py")
    api = _source("app/api/billing.py")
    detail = _source("templates/admin/billing/invoice_detail.html")
    edit = _source("templates/admin/billing/invoice_form.html")
    confirmation = _source("templates/admin/billing/invoice_closure_confirm.html")
    bulk_confirmation = _source(
        "templates/admin/billing/invoice_bulk_void_confirm.html"
    )

    assert '"/invoices/{invoice_id:uuid}/void/preview"' in route
    assert '"/invoices/{invoice_id:uuid}/write-off/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert '"/invoices/{invoice_id}/void/preview"' in api
    assert '"/invoices/{invoice_id}/write-off/preview"' in api
    assert '"/invoices/{invoice_id}/closure-evidence/reconcile"' in api
    assert "invoice_void_capability.allowed" in detail
    assert "invoice_write_off_capability.allowed" in detail
    assert "onsubmit=\"return confirm('Void" not in detail
    assert '<option value="void"' not in edit
    assert '<option value="written_off"' not in edit
    assert '<option value="paid"' not in edit
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact resulting ledger evidence" in confirmation
    assert "does not promise a particular service-access state" in confirmation
    assert "preview_bulk_void" in bulk_route
    assert 'name="preview_fingerprints_json"' in bulk_confirmation


def test_dunning_consumes_arrangement_shields_from_the_arrangement_owner() -> None:
    dunning = _source("app/services/collections/_core.py")

    assert "from app.models.payment_arrangement import" not in dunning
    assert "active_arrangement_shield_reason(db, account_id)" in dunning
    assert "bulk_active_arrangement_shield_reasons(db, ids)" in dunning


def test_dunning_and_restore_use_one_evidenced_access_consequence_owner() -> None:
    dunning = _source("app/services/collections/_core.py")
    event_adapter = _source("app/services/events/handlers/enforcement.py")
    billing_automation = _source("app/services/billing_automation.py")
    billing_settings = _source("templates/admin/system/config/billing.html")
    subscriber_projection = _source("app/services/web_subscriber_details.py")

    assert "def preview_financial_access_consequence(" in dunning
    assert "def confirm_financial_access_consequence(" in dunning
    assert "def preview_financial_access_restoration(" in dunning
    assert "def confirm_financial_access_restoration(" in dunning
    assert "FinancialAccessConsequenceEvidence(" in dunning
    assert "access_consequence=access_consequence" in dunning
    assert "restore_account_services(" in event_adapter
    assert "has_overdue_balance(" not in event_adapter
    assert "_suspension_shield_reason" not in event_adapter
    assert "suspension_warning_sent_at" not in event_adapter
    assert "_emit_dunning_escalations" not in billing_automation
    assert "_emit_post_grace_suspension_escalation" not in billing_automation
    assert "post_grace_suspension" not in billing_automation
    for retired_key in (
        "auto_suspend_on_overdue",
        "suspension_grace_hours",
        "dunning_escalation_days",
        "blocking_period_days",
        "deactivation_period_days",
    ):
        assert f'name="{retired_key}"' not in billing_settings
    assert "next_block_at" not in subscriber_projection
    assert "next_block_label" not in subscriber_projection
