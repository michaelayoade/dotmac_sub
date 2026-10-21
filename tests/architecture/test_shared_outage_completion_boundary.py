"""Real resolver decomposition and shared single-engine boundaries."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_evidence_resolver_has_no_lifecycle_mutation_dependency():
    tree = ast.parse((ROOT / "app/services/topology/outage_completion.py").read_text())
    forbidden = {
        "app.services.topology.outage",
        "app.services.support",
        "app.services.topology.outage_work_order_handoff",
        "app.services.field.completion_review",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden
        if isinstance(node, ast.Import):
            assert not forbidden.intersection(x.name for x in node.names)
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"commit", "rollback", "add", "flush", "emit_event"}


def test_native_correlation_has_no_generic_legacy_receipt_dependency():
    tree = ast.parse((ROOT / "app/services/topology/outage.py").read_text())
    assert not any(
        isinstance(x, ast.ImportFrom) and x.module == "app.models.idempotency"
        for x in ast.walk(tree)
    )


def test_support_and_project_use_one_scoped_acceptance_engine():
    source = (ROOT / "app/services/field/completion_review.py").read_text()
    assert source.count("def _scoped_completion_basis_locked(") == 1
    assert "expected_enrollment_ids" in source
    assert "CompletionEnrollment.subscriber_id == item.subscriber_id" in source
    assert "CompletionEnrollment.project_id == item.project_id" in source
    assert "if not required_ids:" in source


def test_681_freezes_both_issued_audiences_and_complete_shared_basis():
    source = (ROOT / "alembic/versions/681_shared_outage_completion.py").read_text()
    for proof in (
        "outage_complaint_correlation_receipts",
        "support_resolution_shared_incidents",
        "support_resolution_shared_work_scopes",
        "completion_replacement_outage_proofs",
        "replacement_membership_token",
        "shared_outage_request_complete",
        "outage_replacement_proof_complete",
        "outage_issued_scope_immutable",
        "outage_revision_member_immutable",
    ):
        assert proof in source
