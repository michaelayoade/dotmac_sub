from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.models.catalog import BillingMode
from app.services import web_subscription_service_invoicing as web_projection
from app.services.advance_renewal_invoicing import (
    ManualSubscriptionServiceInvoicePreview,
)


def test_page_projection_discloses_prepaid_settlement_consequence(
    db_session, monkeypatch
):
    account_id = uuid4()
    subscription_id = uuid4()
    preview = ManualSubscriptionServiceInvoicePreview(
        account_id=account_id,
        subscription_id=subscription_id,
        account_name="Customer Example",
        account_number="ACC-42",
        plan_name="Unlimited Lite",
        billing_mode=BillingMode.prepaid,
        period_start=datetime(2026, 11, 1, tzinfo=UTC),
        period_end=datetime(2026, 12, 1, tzinfo=UTC),
        currency="NGN",
        subtotal=Decimal("15000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("15000.00"),
        component_count=1,
        fingerprint="a" * 64,
    )
    monkeypatch.setattr(
        web_projection,
        "preview_manual_subscription_service_invoice",
        lambda *_args, **_kwargs: preview,
    )

    page = web_projection.build_subscription_service_invoice_page(
        db_session,
        account_id=account_id,
        subscription_id=subscription_id,
        evaluated_at=datetime(2026, 10, 20, tzinfo=UTC),
    )

    assert page.account_label == "Customer Example · ACC-42"
    assert page.preview.plan_name == "Unlimited Lite"
    assert "one entitlement" in page.settlement_consequence
    assert page.form_contract.submittable is True


def test_customer_subscription_action_is_permission_gated_and_reviewed():
    customer_template = Path("templates/admin/customers/detail.html").read_text()
    review_template = Path(
        "templates/admin/billing/subscription_service_invoice.html"
    ).read_text()
    route = Path("app/web/admin/billing_invoices.py").read_text()

    permission = "billing:invoice:create_subscription_service"
    assert f"can(request, '{permission}')" in customer_template
    assert "/invoices/subscription-service?account_id=" in customer_template
    assert "/invoices/generate-from-subscription" not in customer_template
    assert "require_permission(MANUAL_SUBSCRIPTION_SERVICE_INVOICE_SCOPE)" in route
    assert "create_manual_subscription_service_invoice" in route
    assert "db_session_adapter.release_read_transaction(db)" in route
    assert 'name="preview_fingerprint"' in review_template
    assert 'name="confirmation"' in review_template
    assert 'name="reason"' in review_template
    assert "Issue linked service invoice" in review_template


def test_legacy_admin_subscription_invoice_writer_is_retired():
    route = Path("app/web/admin/billing_invoices.py").read_text()
    adapter = Path("app/services/web_billing_invoices.py").read_text()

    assert "/invoices/generate-from-subscription" not in route
    assert "generate_invoice_from_subscription_web" not in route
    assert "generate_invoice_from_subscription_web" not in adapter
    assert "Invoices.create_for_subscription" not in route
