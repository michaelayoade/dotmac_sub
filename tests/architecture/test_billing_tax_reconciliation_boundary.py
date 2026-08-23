"""Legacy VAT reconciliation stays read-only and delegates money evidence."""

from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_reconciliation_owner_is_bounded_and_read_only() -> None:
    source = _read("app/services/billing_tax_reconciliation.py")
    service = service_relationship("financial.billing_tax_reconciliation")

    assert service.module == "app.services.billing_tax_reconciliation"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.READ_ONLY
    assert ".limit(bounded_limit + 1)" in source
    for forbidden in (
        "db.add(",
        "db.flush(",
        "db.commit(",
        "db.rollback(",
        "Invoice(",
        "CreditNote(",
    ):
        assert forbidden not in source


def test_correction_coordinator_delegates_to_credit_note_owner() -> None:
    source = _read("app/services/web_billing_tax_reconciliation.py")
    general_credit = _read("app/services/web_billing_credits.py")

    assert "CreditNotes.preview_issue(" in source
    assert "CreditNotes.issue_with_evidence(" in source
    assert "candidate.can_prepare_tax_credit" in source
    assert "hmac.compare_digest" in source
    assert "CreditNote(" not in source
    assert "Invoice(" not in source
    assert "db.add(" not in source
    assert 'tax_total=Decimal("0.00")' in general_credit
    assert "tax_total=candidate.maximum_remaining_adjustment" in source
    assert "subtotal=0" in source


def test_tax_reconciliation_routes_are_thin_and_guarded() -> None:
    source = _read("app/web/admin/billing_credits.py")
    start = source.index("def billing_tax_reconciliation(")
    end = source.index('\n\n@router.get(\n    "/credits/new"', start)
    reconciliation_routes = source[start:end]

    assert "web_billing_tax_reconciliation_service" in reconciliation_routes
    assert 'require_permission("billing:tax:read")' in source
    assert 'require_permission("billing:credit_note:create")' in source
    for forbidden in ("db.query(", "db.execute(", "select(", "CreditNote("):
        assert forbidden not in reconciliation_routes


def test_operator_ui_preserves_evidence_and_requires_explicit_confirmation() -> None:
    queue = _read("templates/admin/billing/tax_reconciliation.html")
    confirmation = _read(
        "templates/admin/billing/tax_reconciliation_credit_confirm.html"
    )

    assert "does not rewrite issued invoices" in queue
    assert "maximum exposure, not a confirmed refund" in queue
    assert 'name="candidate_fingerprint"' in queue
    assert 'type="hidden" name="preview_fingerprint"' in confirmation
    assert 'type="hidden" name="idempotency_key"' in confirmation
    assert "Does not edit, void, replace" in confirmation
    assert "Does not apply the credit" in confirmation
    assert "Issue {{ review.candidate.currency }}" in confirmation
