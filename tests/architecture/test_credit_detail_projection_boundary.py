"""Credit drill-down is a typed projection, never a second money owner."""

import ast
import runpy
from dataclasses import replace
from pathlib import Path
from typing import cast

from app.services.sot_manifest import (
    OwnerRole,
    SOTService,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_registry.model import DomainSOT

ROOT = Path(__file__).resolve().parents[2]


def _service() -> SOTService:
    declaration = runpy.run_path(
        str(ROOT / "app/services/sot_registry/domains/ui_credit_detail_projection.py")
    )
    return cast(DomainSOT, declaration["DOMAIN"]).services[0]


def test_credit_detail_projection_contract_is_complete_and_read_only() -> None:
    service = _service()
    assert service.name == "ui.credit_detail_projection"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.READ_ONLY
    assert all(
        concern.role is OwnerRole.RESOLVER for concern in service.contract.concerns
    )
    assert service.depends_on == ("financial.credit_notes", "ui.status_presentation")
    assert (
        contract_validation_errors(
            service,
            service_names={service.name, *service.depends_on},
        )
        == ()
    )
    declarations = (ROOT / "app/services/sot_registry/domains/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "UI_CREDIT_DETAIL_PROJECTION," in declarations


def test_credit_projection_contract_guard_rejects_a_second_financial_authority() -> (
    None
):
    service = _service()
    contract = service.contract
    assert contract is not None
    broken_input = replace(
        contract.authoritative_inputs[0], owner="financial.parallel_credit"
    )
    broken = replace(
        service,
        contract=replace(
            contract,
            authoritative_inputs=(broken_input, *contract.authoritative_inputs[1:]),
        ),
    )
    assert contract_validation_errors(
        broken, service_names={service.name, *service.depends_on}
    )


def test_credit_detail_projection_calls_only_the_incumbent_read_owner() -> None:
    source = (ROOT / "app/services/web_billing_credits.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    projection = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_credit_detail"
    )
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(projection)
        if isinstance(node, ast.Call)
    }
    assert "billing_service.credit_notes.get" in calls
    assert not any(
        call.endswith((".commit", ".flush", ".add", ".execute", ".query"))
        for call in calls
    )
    assert not any(isinstance(node, ast.BinOp) for node in ast.walk(projection))
    assert ast.unparse(projection.returns) == "CreditDetailView"
