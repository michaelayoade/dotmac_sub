from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship


def test_invoice_draft_authoring_has_one_contracted_atomic_owner() -> None:
    service = service_relationship("financial.invoice_draft_authoring")

    assert service.module == "app.services.invoice_draft_authoring"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert {concern.name for concern in service.contract.concerns} == {
        "administrative invoice draft authoring coordination",
        "administrative proforma conversion coordination",
    }
    assert all(
        concern.role is OwnerRole.APPLICATION_COORDINATOR
        for concern in service.contract.concerns
    )


def test_admin_invoice_form_does_not_write_headers_or_lines_directly() -> None:
    source = Path("app/services/web_billing_invoices.py").read_text()
    create_start = source.index("def create_invoice_from_form(")
    update_end = source.index("def create_invoice_web(")
    authoring_adapter = source[create_start:update_end]

    assert "billing_service.invoices.create(" not in authoring_adapter
    assert "billing_service.invoices.update(" not in authoring_adapter
    assert "billing_service.invoice_lines.create(" not in authoring_adapter
    assert "billing_service.invoice_lines.update(" not in authoring_adapter
    assert "billing_service.invoice_lines.delete(" not in authoring_adapter
    assert "invoice_draft_authoring.create_invoice_draft(" in authoring_adapter
    assert "invoice_draft_authoring.update_invoice_draft(" in authoring_adapter


def test_admin_proforma_conversion_delegates_to_locked_owner() -> None:
    source = Path("app/services/web_billing_invoices.py").read_text()
    conversion_start = source.index("def convert_proforma_to_final(")
    conversion_end = source.index("def build_invoice_payload_data(")
    conversion_adapter = source[conversion_start:conversion_end]

    assert "invoice_draft_authoring.convert_proforma_invoice(" in conversion_adapter
    assert "billing_service.invoices.update(" not in conversion_adapter
    assert "db.commit(" not in conversion_adapter


def test_prepaid_proforma_conversion_guard_lives_in_owner() -> None:
    source = Path("app/services/invoice_draft_authoring.py").read_text()
    conversion_start = source.index("def _convert_proforma_invoice(")
    conversion_owner = source[conversion_start:]

    assert "_requires_prepaid_reconciliation(db, invoice)" in conversion_owner
    assert '"prepaid_reconciliation_required"' in conversion_owner


def test_account_vat_selection_is_shared_by_draft_and_form() -> None:
    owner = Path("app/services/invoice_draft_authoring.py").read_text()
    route = Path("app/web/admin/billing_invoices.py").read_text()
    template = Path("templates/admin/billing/invoice_form.html").read_text()
    calculator = Path("static/js/invoice-calculator.js").read_text()

    assert "def resolve_account_vat_selection(" in owner
    assert "account_vat = resolve_account_vat_selection(" in owner
    assert "resolve_account_vat_selection(db, account=account)" in route
    assert ':disabled="accountVatLocked"' in template
    assert "already has VAT configured" in template
    assert "previousAccountVatRateId" in calculator
