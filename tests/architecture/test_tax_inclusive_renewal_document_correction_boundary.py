from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_tax_inclusive_document_correction_is_registered_typed_owner() -> None:
    owner = _source("app/services/prepaid_service_renewals.py")
    registry = _source("app/services/sot_registry/domains/financial_access/prepaid.py")
    assert "class TaxInclusiveRenewalDocumentCorrectionQuery" in owner
    assert "class TaxInclusiveRenewalDocumentCorrectionPreview" in owner
    assert "class CorrectTaxInclusiveRenewalDocumentCommand" in owner
    assert "class TaxInclusiveRenewalDocumentCorrectionResult" in owner
    assert "def preview_tax_inclusive_renewal_document_correction(" in owner
    assert "def correct_tax_inclusive_renewal_document(" in owner
    assert owner.count("definition=_TAX_INCLUSIVE_DOCUMENT_CORRECTION_COMMAND") == 1
    assert "reviewed tax-inclusive prepaid renewal documentary correction" in registry


def test_document_correction_cli_only_delegates_to_typed_owner() -> None:
    source = _source("scripts/billing/correct_tax_inclusive_renewal_document.py")
    tree = ast.parse(source)
    forbidden = {"commit", "rollback", "begin", "begin_nested"}
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert calls.isdisjoint(forbidden)
    assert "preview_tax_inclusive_renewal_document_correction" in source
    assert "correct_tax_inclusive_renewal_document" in source


def test_documentary_sequence_uses_explicit_typed_entity_arguments() -> None:
    combined = "\n".join(
        (
            _source("app/services/prepaid_service_renewals.py"),
            _source("app/services/prepaid_draft_reconciliation.py"),
            _source("scripts/billing/correct_tax_inclusive_renewal_document.py"),
            _source("scripts/billing/reconcile_prepaid_drafts.py"),
        )
    )
    assert "--account-id" in combined
    assert "--subscription-id" in combined
    assert "--payment-id" in combined
    assert "customer_name ==" not in combined
    assert "subscriber_number ==" not in combined
