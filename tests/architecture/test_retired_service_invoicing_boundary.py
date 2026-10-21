"""Retired creation paths cannot return through an adapter or parallel owner."""

from pathlib import Path

from app.services.sot_registry.registry import all_services

ROOT = Path(__file__).resolve().parents[2]


def test_new_creation_owners_and_routes_are_removed() -> None:
    services = {service.name: service for service in all_services()}
    assert "financial.backdated_service_invoicing" not in services
    assert "reviewed next-period subscription service invoice" not in (
        services["financial.advance_renewal_invoicing"].owns
    )
    assert not (ROOT / "app/services/backdated_service_invoicing.py").exists()
    assert not (ROOT / "app/services/web_subscription_service_invoicing.py").exists()
    route = (ROOT / "app/web/admin/billing_invoices.py").read_text(encoding="utf-8")
    assert "backdated-service" not in route
    assert "subscription-service" not in route
    assert "generate-from-subscription" not in route
    assert "generate_invoice_from_subscription_web" not in (
        ROOT / "app/services/web_billing_invoices.py"
    ).read_text(encoding="utf-8")


def test_retired_actions_are_absent_from_templates_and_permission_seeds() -> None:
    for relative in (
        "templates/admin/customers/detail.html",
        "templates/admin/billing/invoice_detail.html",
        "scripts/seed/seed_rbac.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for permission in (
            "billing:invoice:create_subscription_service",
            "billing:invoice:create_backdated_service",
            "billing:invoice:reconstruct_backdated_service",
        ):
            assert permission not in source


def test_applied_permission_migrations_and_historical_interpretation_remain() -> None:
    for relative, revision in (
        (
            "alembic/versions/689_forward_subscription_service_invoice_permission.py",
            "689_forward_service_invoice_permission",
        ),
        (
            "alembic/versions/690_backdated_invoice_permissions.py",
            "690_backdated_invoice_permissions",
        ),
    ):
        assert revision in (ROOT / relative).read_text(encoding="utf-8")
    invoice_owner = (ROOT / "app/services/billing/invoices.py").read_text(
        encoding="utf-8"
    )
    assert "_assert_controlled_draft_ready_for_issue(db, invoice)" in invoice_owner
    assert "_assert_generic_draft_mutation_allowed(invoice)" in invoice_owner
    assert "stage_reviewed_backdated_service_line_for_owner" not in invoice_owner
    assert "reserve_backdated_service_idempotency_for_owner" not in invoice_owner
