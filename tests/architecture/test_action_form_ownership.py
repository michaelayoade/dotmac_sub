from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_payment_proof_template_only_renders_declared_action_forms() -> None:
    template = _read("templates/admin/billing/payment_proof_detail.html")

    assert 'components/forms/action_form.html" import action_form' in template
    assert "{% for review_action in review_actions %}" in template
    assert "{{ action_form(review_action) }}" in template
    assert "if st == 'submitted'" not in template
    assert 'name="amount"' not in template
    assert 'name="auto_allocate"' not in template
    assert "return confirm('" not in template


def test_shared_renderer_exposes_accessible_contract_semantics() -> None:
    template = _read("templates/components/forms/action_form.html")
    design_system = _read("static/css/design-system.css")
    base = _read("templates/base.html")

    for marker in (
        "aria-labelledby",
        "aria-describedby",
        "aria-invalid",
        'aria-live="assertive"',
        'role="alert"',
        'data-confirm-message="{{ form.confirmation.message }}"',
        "{% if not form.allowed %}disabled",
        'include "components/forms/csrf_input.html"',
    ):
        assert marker in template
    assert "form.tone.value" in template
    assert "bg-emerald" not in template
    assert "bg-rose" not in template
    assert ".action-form-submit" in design_system
    assert "background: var(--status-indicator)" in design_system
    assert "/static/css/design-system.css?v=20260714a" in base


def test_payment_proof_projection_delegates_eligibility_to_command_owner() -> None:
    web_projection = _read("app/services/web_billing_payment_proofs.py")
    command_owner = _read("app/services/payment_proofs.py")
    route = _read("app/web/admin/billing_payment_proofs.py")

    assert "payment_proofs_service.review_eligibility(" in web_projection
    assert "class PaymentProofReviewEligibility" in command_owner
    assert "class PaymentProofReviewError" in command_owner
    assert 'has_permission(auth, db, "billing:proof:verify")' in route
    assert "PaymentProofStatus" not in route


def test_checked_in_sources_name_action_form_owner_and_migration() -> None:
    registry = _read("app/services/sot_relationships.py")
    relationships = _read("docs/SOT_RELATIONSHIP_MAP.md")
    frontend = _read("docs/FRONTEND_SPEC.md")

    assert 'name="ui.action_form_contracts"' in registry
    assert 'name="ui.payment_proof_review_projection"' in registry
    assert "## UI Action Forms" in relationships
    assert "Old owner: payment-proof detail Jinja" in relationships
    assert "### Server-owned action forms" in frontend
