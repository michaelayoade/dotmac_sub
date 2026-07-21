"""Guard configuration and ownership boundaries for prepaid enforcement."""

from pathlib import Path

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import SETTINGS_SPECS
from app.services.sot_relationships import all_services

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_prepaid_minimum_has_one_canonical_setting_owner():
    owners = {
        spec.domain
        for spec in SETTINGS_SPECS
        if spec.key == "prepaid_default_min_balance"
    }
    assert owners == {SettingDomain.billing}


def test_planner_consumes_config_owners_instead_of_local_policy_values():
    planner = _read("app/services/prepaid_enforcement_planner.py")
    assert "resolve_prepaid_enforcement_policy" in planner
    assert "resolve_grace_decision" in planner
    assert "resolve_prepaid_funding" in planner
    assert "timedelta(" not in planner


def test_prepaid_enforcement_has_a_complete_read_only_manifest():
    service = next(
        item for item in all_services() if item.name == "financial.prepaid_enforcement"
    )

    assert service.is_contracted
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "read_only"
    assert service.contract.migration.state.value == "complete"
    assert {concern.name for concern in service.contract.concerns} == set(service.owns)
    assert service.contract.errors.domain_codes
    assert service.contract.errors.fail_closed_on


def test_planner_exposes_typed_outcomes_and_transport_neutral_failures():
    planner = _read("app/services/prepaid_enforcement_planner.py")

    assert "class PrepaidEnforcementAction(StrEnum)" in planner
    assert "class PrepaidEnforcementPolicyIssue(StrEnum)" in planner
    assert "class PrepaidEnforcementReasonSource(StrEnum)" in planner
    assert "class PrepaidEnforcementError(DomainError)" in planner
    assert "account_status: SubscriberStatus" in planner
    assert "billing_mode: BillingMode | None" in planner
    assert "dict[str, Any]" not in planner
    assert "set[Any]" not in planner
    assert "list[Any]" not in planner
    assert "raise ValueError" not in planner
    assert "HTTPException" not in planner
    assert ".commit(" not in planner
    assert ".rollback(" not in planner


def test_readiness_is_a_gate_not_a_runtime_balance_source():
    readiness = _read("app/services/prepaid_enforcement_readiness.py")
    sweep = _read("app/services/collections/prepaid_balance_sweep.py")
    assert "record_prepaid_enforcement_readiness" in readiness
    assert "reconstruction_evidence_sha256" in readiness
    assert "funding_decisions_hash" in readiness
    assert "prepaid_enforcement_readiness_block_reason" in sweep
    assert "available_balance=record" not in sweep
    assert "required_balance=record" not in sweep


def test_prepaid_suspension_has_one_runtime_adapter():
    callers = []
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/services/collections/_core.py":
            continue
        if "_suspend_account(" in path.read_text():
            callers.append(relative)
    assert callers == ["app/services/collections/prepaid_balance_sweep.py"]
