from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from app.services import web_prepaid_coverage_reconciliation as web_reconciliation
from app.services.prepaid_coverage_reconciliation import (
    CoverageReconciliationDecision,
    CoverageReconciliationReason,
    CoverageReconciliationSource,
    PrepaidCoverageInvoiceReconciliationPreview,
    PrepaidCoverageReconciliationPreview,
    PrepaidCoverageReconciliationPreviewItem,
)

INVOICE_ID = UUID("11111111-1111-1111-1111-111111111111")
LINE_ID = UUID("22222222-2222-2222-2222-222222222222")
SUBSCRIPTION_ID = UUID("33333333-3333-3333-3333-333333333333")
ACCOUNT_ID = UUID("44444444-4444-4444-4444-444444444444")
ACTOR = "system_user:55555555-5555-5555-5555-555555555555"
NOW = datetime(2026, 8, 24, 12, 30, tzinfo=UTC)
FINGERPRINT = "a" * 64


def _review() -> PrepaidCoverageInvoiceReconciliationPreview:
    preview = PrepaidCoverageReconciliationPreview(
        as_of=NOW,
        subscription_ids=(SUBSCRIPTION_ID,),
        items=(
            PrepaidCoverageReconciliationPreviewItem(
                subscription_id=SUBSCRIPTION_ID,
                account_id=ACCOUNT_ID,
                decision=CoverageReconciliationDecision.entitlement_created,
                reason=CoverageReconciliationReason.exact_paid_invoice_line,
                source=CoverageReconciliationSource.invoice_line,
                source_id=LINE_ID,
                starts_at=NOW,
                ends_at=NOW,
                amount=None,
                currency="NGN",
                evidence_fingerprint="b" * 64,
            ),
        ),
        fingerprint=FINGERPRINT,
    )
    return PrepaidCoverageInvoiceReconciliationPreview(
        invoice_id=INVOICE_ID,
        invoice_line_ids=(LINE_ID,),
        preview=preview,
    )


def _claims() -> dict[str, object]:
    return {
        "typ": "prepaid_coverage_reconciliation_confirmation",
        "iss": "dotmac_sub.admin.prepaid_coverage_reconciliation",
        "ver": 1,
        "jti": "6" * 32,
        "actor": ACTOR,
        "invoice_id": str(INVOICE_ID),
        "preview_fingerprint": FINGERPRINT,
        "as_of": int(NOW.timestamp()),
    }


def test_invoice_detail_exposes_only_exact_paid_invoice_repair(monkeypatch):
    monkeypatch.setattr(
        web_reconciliation,
        "preview_prepaid_coverage_reconciliation_for_invoice",
        lambda *_args, **_kwargs: _review(),
    )

    state = web_reconciliation.preview_for_invoice_detail(
        object(), invoice_id=INVOICE_ID
    )

    assert state is not None
    assert state.actionable is True
    assert "no corresponding prepaid service entitlement" in state.reason


def test_confirm_builds_fingerprint_bound_owner_command(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        web_reconciliation.context_signing,
        "verify_context_token",
        lambda *_args: _claims(),
    )
    monkeypatch.setattr(
        web_reconciliation,
        "preview_prepaid_coverage_reconciliation_for_invoice",
        lambda *_args, **_kwargs: _review(),
    )
    monkeypatch.setattr(
        web_reconciliation.db_session_adapter,
        "release_read_transaction",
        lambda _db: None,
    )

    def _reconcile(_db, command):
        captured["command"] = command
        return SimpleNamespace()

    monkeypatch.setattr(
        web_reconciliation, "reconcile_prepaid_service_coverage", _reconcile
    )

    web_reconciliation.confirm_admin_review(
        object(),
        invoice_id=INVOICE_ID,
        actor=ACTOR,
        preview_fingerprint=FINGERPRINT,
        confirmation_token="signed-review-token",
        confirmed="yes",
        reason="Reviewed exact paid invoice coverage evidence",
    )

    command = captured["command"]
    assert command.preview_fingerprint == FINGERPRINT
    assert command.subscription_ids == (SUBSCRIPTION_ID,)
    assert command.context.actor == ACTOR
    assert command.context.idempotency_key == f"prepaid-coverage-admin:{'6' * 32}"


def test_invoice_page_template_and_routes_use_the_owner_action_form():
    from pathlib import Path

    detail = Path("templates/admin/billing/invoice_detail.html").read_text()
    confirmation = Path(
        "templates/admin/billing/prepaid_coverage_repair_confirm.html"
    ).read_text()
    route = Path("app/web/admin/billing_invoice_actions.py").read_text()

    assert "prepaid_coverage_reconciliation_state" in detail
    assert "prepaid-coverage-reconciliation/preview" in detail
    assert "Finance review is required" in detail
    assert "action_form(review.action_form)" in confirmation
    assert "prepaid-coverage-reconciliation/confirm" in route
    assert 'require_permission("billing:invoice:update")' in route
