"""Keep captive access decisions behind one fail-closed policy owner."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "walled_garden_policy.py"


def test_walled_garden_policy_has_a_complete_read_only_manifest() -> None:
    service = next(
        item for item in all_services() if item.name == "access.walled_garden_policy"
    )

    assert service.is_contracted
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "read_only"
    assert service.contract.migration.state.value == "complete"
    assert {concern.name for concern in service.contract.concerns} == set(service.owns)
    assert service.contract.errors.domain_codes == ()
    assert service.contract.errors.fail_closed_on


def test_policy_exposes_typed_reasons_and_has_no_transport_or_transaction_code() -> (
    None
):
    source = OWNER.read_text(encoding="utf-8")

    assert "class WalledGardenReason(StrEnum)" in source
    assert "reason: WalledGardenReason" in source
    assert '"reason": self.reason.value' in source
    assert source.count('"captive_ready"') == 1
    assert "HTTPException" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_all_captive_outcomes_are_explicitly_fail_closed_or_ready() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "effective_mode=AccessRestrictionMode.captive" in source
    assert source.count("effective_mode=AccessRestrictionMode.hard_reject") == 2
    assert "if AccessRestrictionMode.hard_reject in modes:" in source
    assert "elif modes and all(" in source
    assert (
        "# Historical restrictions without structured evidence fail closed." in source
    )
