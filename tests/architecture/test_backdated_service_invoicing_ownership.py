from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_backdated_service_invoice_has_one_registered_typed_owner():
    declaration = _read("app/services/sot_registry/domains/financial_access/prepaid.py")
    owner = _read("app/services/backdated_service_invoicing.py")

    assert 'name="financial.backdated_service_invoicing"' in declaration
    assert "contract=ServiceContract(" in declaration
    assert "execute_owner_command(" in owner
    assert "Invoices.stage_system_invoice_for_owner(" in owner
    assert "InvoiceLines.stage_reviewed_backdated_service_line_for_owner(" in owner
    assert "SystemInvoiceLineCreate(" not in owner
    assert "Invoices.adopt_reviewed_prepaid_sequence_document_for_owner(" in owner


def test_routes_and_payment_consequence_use_dedicated_contracts():
    routes = _read("app/web/admin/billing_invoices.py")
    payments = _read("app/services/billing/payments.py")

    assert "backdated_service_invoicing.CREATE_PERMISSION" in routes
    assert "backdated_service_invoicing.RECONSTRUCT_PERMISSION" in routes
    assert "backdated-service-invoicing:" in payments
    assert "reviewed_backdated_service_document" in payments


def test_controlled_draft_is_sealed_and_revalidated_by_invoice_owner():
    owner = _read("app/services/backdated_service_invoicing.py")
    invoices = _read("app/services/billing/invoices.py")

    assert "Invoices.seal_controlled_draft_for_owner(" in owner
    assert "ControlledInvoiceDraftSeal(" in owner
    assert "owner_command_active(db, owner=controller_owner)" in invoices
    assert "_assert_backdated_service_line_evidence_owner(" in invoices
    assert "_assert_controlled_draft_ready_for_issue(db, invoice)" in invoices
    assert "financial.invoice.controlled_draft_drift" in invoices


def test_permission_migration_extends_current_head_without_default_grants():
    migration = _read("alembic/versions/690_backdated_invoice_permissions.py")
    upgrade_source = migration.split("def downgrade()", 1)[0]

    assert 'revision: str = "690_backdated_invoice_permissions"' in migration
    assert (
        'down_revision: str | None = "689_forward_service_invoice_permission"'
        in migration
    )
    assert (
        'down_revision: str | None = "686_field_lifecycle_router_health_merge"'
        not in migration
    )
    assert "is_ui_assignable=True" in upgrade_source
    assert "role_permissions" not in upgrade_source
    assert "subscriber_permissions" not in upgrade_source
    assert "system_user_permissions" not in upgrade_source
    assert "class DowngradeRefused" in migration


def test_generic_invoice_form_does_not_claim_subscription_period_identity():
    form = _read("templates/admin/billing/invoice_form.html")

    assert "subscription_id" not in form
    assert "billing_period_start" not in form
