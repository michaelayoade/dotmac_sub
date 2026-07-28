"""Pin non-standard subscription billing to one explicit authority boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/subscription_billing_treatments.py"
GRANT_OWNER = ROOT / "app/services/subscription_billing_grants.py"
API = ROOT / "app/api/billing_treatments.py"
SCHEMA = ROOT / "app/schemas/subscription_billing_treatment.py"
SETTINGS = ROOT / "app/services/settings_spec.py"
MIGRATION = ROOT / "alembic/versions/399_subscription_billing_treatments.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(_source(path), filename=str(path))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _attribute_calls(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def test_treatment_and_grant_owners_have_complete_typed_contracts() -> None:
    services = {item.name for item in sot_relationships.all_services()}
    treatment = sot_relationships.service_relationship(
        "financial.subscription_billing_treatments"
    )
    grants = sot_relationships.service_relationship(
        "financial.subscription_billing_grants"
    )
    assert treatment.contract is not None
    assert grants.contract is not None
    assert treatment.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert grants.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert treatment.contract.migration.state is AuthorityMigrationState.CUTOVER_READY
    assert grants.contract.migration.state is AuthorityMigrationState.CUTOVER_READY
    assert {item.role for item in treatment.contract.concerns} == {
        OwnerRole.COMMAND_WRITER,
        OwnerRole.POLICY,
    }
    assert {item.role for item in grants.contract.concerns} == {
        OwnerRole.AUTHORITATIVE_RECORD,
        OwnerRole.PROJECTION_WRITER,
    }
    assert contract_validation_errors(treatment, service_names=services) == ()
    assert contract_validation_errors(grants, service_names=services) == ()


def test_grant_is_flush_only_and_lifecycle_commands_own_transactions() -> None:
    source = _source(OWNER)
    grant_source = _source(GRANT_OWNER)
    grant = _function(GRANT_OWNER, "stage_subscription_billing_grant")
    assert source.count("execute_owner_command(") == 2
    assert "execute_owner_command(" not in grant_source
    assert "commit" not in _attribute_calls(grant)
    assert "rollback" not in _attribute_calls(grant)
    assert "execute_owner_command" not in ast.unparse(grant)


def test_administrative_routes_are_thin_permissioned_adapters() -> None:
    source = _source(API)
    assert 'require_permission("billing:treatment:read")' in source
    assert source.count('require_permission("billing:treatment:write")') == 3
    assert "db.add(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_customer_money_paths_consume_the_treatment_owner() -> None:
    postpaid = _source(ROOT / "app/services/billing_automation.py")
    prepaid = _source(ROOT / "app/services/prepaid_service_renewals.py")
    threshold = _source(ROOT / "app/services/prepaid_threshold.py")
    plan_change = _source(ROOT / "app/services/catalog/subscriptions.py")
    for source in (postpaid, prepaid):
        assert "resolve_subscription_billing_treatments" in source
        assert "stage_subscription_billing_grant" in source
    assert "resolve_subscription_billing_treatments" in threshold
    assert "non_billable_subscription_ids" in threshold
    assert "subscription_has_open_billing_treatment" in plan_change


def test_zero_price_offers_are_not_customer_self_service_change_targets() -> None:
    portal_context = _source(ROOT / "app/services/customer_portal_context.py")
    portal_changes = _source(ROOT / "app/services/customer_portal_flow_changes.py")
    guard = "offer_has_positive_recurring_price"
    assert guard in portal_context
    assert guard in portal_changes
    assert "PriceType.recurring" in portal_context
    assert "len(recurring_prices) == 1" in portal_context


def test_migration_preserves_price_and_makes_grants_append_only() -> None:
    source = _source(MIGRATION)
    assert 'down_revision = "398_permanent_financial_lifecycle"' in source
    assert '"billing:treatment:read"' in source
    assert '"billing:treatment:write"' in source
    assert '"billing_cycle"' in source
    assert "source_billing_grant_id" in source
    assert "BEFORE UPDATE OR DELETE ON subscription_billing_grants" in source
    assert "protect_subscription_billing_treatment_terms" in source
    assert "BEFORE UPDATE OF offer_id" in source
    assert "maximum_recurring_amount > 0" in source
    assert "reference_amount > 0" in source


def test_treatments_are_finite_and_periodic_reapproval_is_policy_owned() -> None:
    owner = _source(OWNER)
    schema = _source(SCHEMA)
    settings = _source(SETTINGS)
    migration = _source(MIGRATION)
    setting = "subscription_billing_treatment_max_days"
    assert setting in owner
    assert setting in settings
    assert "ends_at: datetime\n" in schema
    assert (
        'sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False)' in migration
    )
    assert "approval_policy_max_days BETWEEN 1 AND 366" in migration
    assert "finite_period_required" in owner
    assert "approval_horizon_exceeded" in owner


def test_zero_price_and_account_flags_are_not_treatment_authority() -> None:
    owner = _source(OWNER)
    design = _source(ROOT / "docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md")
    assert 'amount <= Decimal("0.00")' in owner
    assert "billing_enabled" not in owner
    assert "A zero-price catalog offer is valid only for a product" in design
    assert "are not complimentary-service authority" in design
