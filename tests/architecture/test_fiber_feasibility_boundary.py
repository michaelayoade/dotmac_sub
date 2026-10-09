"""Coverage authority and transaction-isolation guards."""

from pathlib import Path

from app.services.sot_relationships import all_services


def test_fiber_feasibility_has_one_typed_read_owner():
    owner = next(s for s in all_services() if s.name == "sales.fiber_feasibility")
    assert owner.contract is not None
    assert owner.contract.transaction.mode.value == "read_only"
    assert "def compute_feasibility(" not in Path(
        "app/services/sales/selfserve.py"
    ).read_text(encoding="utf-8")


def test_optional_coverage_uses_owner_savepoint():
    source = Path("app/services/team_inbox_receive.py").read_text(encoding="utf-8")
    assert "feasibility = execute_owner_savepoint(" in source
    assert "pin = fiber_feasibility.FiberFeasibilityQuery(" in source
    assert '"status": "technical_error"' in source
    assert ".begin_nested(" not in source
