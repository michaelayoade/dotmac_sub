"""Pin payment-provider observations to one typed canonical owner."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import TransactionMode, contract_validation_errors

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/payment_provider_events.py"
CONFIG = ROOT / "app/services/billing/providers.py"
API = ROOT / "app/api/billing.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(path: Path, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def test_payment_provider_event_owner_has_complete_contract() -> None:
    service = sot_relationships.service_relationship(
        "financial.payment_provider_events"
    )

    assert service.module == "app.services.payment_provider_events"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert not contract_validation_errors(
        service,
        service_names={item.name for item in sot_relationships.all_services()},
    )
    baseline = (ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt").read_text(
        encoding="utf-8"
    )
    assert "financial.payment_provider_events" not in baseline.splitlines()


def test_public_command_and_verified_participants_are_transaction_safe() -> None:
    source = OWNER.read_text(encoding="utf-8")
    ingest = _function(OWNER, "ingest")
    webhook = _function(OWNER, "stage_verified_webhook_event")
    reconciliation = _function(OWNER, "stage_verified_reconciliation_event")

    assert source.count("execute_owner_command(") == 1
    assert "PaymentProviderEventCommand" in source
    assert "CommandContext" in source
    assert "HTTPException" not in source
    assert "fastapi" not in source
    assert "trusted_financial_effects" not in source
    assert "stage_ingest" not in source
    for participant in (ingest, webhook, reconciliation):
        assert "commit" not in _calls(participant)
        assert "rollback" not in _calls(participant)
        assert "begin_nested" not in _calls(participant)


def test_configuration_module_has_no_parallel_event_owner() -> None:
    source = CONFIG.read_text(encoding="utf-8")
    assert "class PaymentProviderEvents" not in source
    assert "PaymentProviderEvent(" not in source
    assert "trusted_financial_effects" not in source


def test_api_is_a_typed_transaction_neutral_adapter() -> None:
    source = API.read_text(encoding="utf-8")
    route = _function(API, "ingest_payment_event")
    calls = _calls(route)

    assert "PaymentProviderEventCommand" in source
    assert "CommandContext" in source
    assert "release_read_transaction" in calls
    assert "commit" not in calls
    assert "rollback" not in calls
    assert "trusted_financial_effects" not in source


def test_provider_event_record_persists_trust_and_replay_evidence() -> None:
    model = (ROOT / "app/models/billing.py").read_text(encoding="utf-8")
    migration = (
        ROOT / "alembic/versions/395_payment_provider_event_provenance.py"
    ).read_text(encoding="utf-8")

    for field in (
        "source",
        "observation_digest",
        "observed_payment_status",
        "provider_fee",
        "net_amount",
        "provider_reference",
        "error_code",
    ):
        assert field in model
        assert field in migration


def test_verified_source_guard_uses_only_old_new_and_cannot_bypass_rls() -> None:
    path = ROOT / "alembic/versions/693_provider_source_facts.py"
    tree = _tree(path)
    source = path.read_text(encoding="utf-8")
    revision = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "revision"
    )
    assert isinstance(revision.value, ast.Constant)
    assert len(revision.value.value) <= 32
    sql = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "CREATE FUNCTION" in node.value
    )
    assert "SECURITY INVOKER SET search_path = pg_catalog" in sql
    assert "SECURITY DEFINER" not in source
    for forbidden in (
        "SELECT ",
        " JOIN ",
        "PERFORM ",
        "EXECUTE ",
        "module_invoice_cash_correlations",
        "app_current_tenant_id",
    ):
        assert forbidden not in sql
    for field in (
        "id",
        "provider_id",
        "event_type",
        "external_id",
        "idempotency_key",
        "source",
        "observation_digest",
        "observed_payment_status",
        "amount",
        "provider_fee",
        "net_amount",
        "provider_reference",
        "currency",
        "financial_effect",
        "occurred_at",
        "received_at",
    ):
        assert f"OLD.{field}" in sql
        assert f"NEW.{field}" in sql
    assert "OLD.source IN ('verified_webhook', 'gateway_reconciliation')" in sql
    assert "IS DISTINCT FROM" in sql
    assert "ENABLE ALWAYS TRIGGER" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source
    assert "BEFORE INSERT" not in source
    downgrade = ast.unparse(_function(path, "downgrade"))
    assert "RuntimeError" in downgrade
    assert "forward migration" in downgrade
    assert "DROP" not in downgrade
    assert "execute" not in _calls(_function(path, "downgrade"))
    for mutable in (
        "payload",
        "payment_id",
        "invoice_id",
        "status",
        "processed_at",
        "error_code",
        "error",
    ):
        assert f"NEW.{mutable}" not in sql


def test_pending_verified_replay_keeps_first_admission_source() -> None:
    admit = _function(OWNER, "_admit_event")
    assignments = [
        node
        for node in ast.walk(admit)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "existing"
            and target.attr == "source"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assignment = assignments[0]
    guard = next(
        node
        for node in ast.walk(admit)
        if isinstance(node, ast.If)
        and any(child is assignment for child in ast.walk(node))
        and "existing.source not in" in ast.unparse(node.test)
    )
    assert "PaymentProviderEventSource.verified_webhook" in ast.unparse(guard.test)
    assert "PaymentProviderEventSource.gateway_reconciliation" in ast.unparse(
        guard.test
    )
    assert "existing.payload = observation.payload" in ast.unparse(admit)


def test_cash_source_query_keeps_requested_credit_distinct_from_provider_net() -> None:
    path = ROOT / "app/services/provider_payment_settlements.py"
    query = ast.unparse(_function(path, "get_module_invoice_cash_source"))
    assert "net != frozen.requested.amount" not in query
    assert "requested_customer_credit_amount=frozen.requested.amount" in query
    assert "gross_amount=gross" in query
    assert "provider_fee=fee" in query
    assert "net_amount=net" in query
