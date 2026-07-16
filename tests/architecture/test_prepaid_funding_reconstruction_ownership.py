"""Guard the final prepaid funding authority boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _read_migration(suffix: str) -> str:
    matches = list((ROOT / "alembic" / "versions").glob(f"*_{suffix}.py"))
    assert len(matches) == 1, matches
    return matches[0].read_text(encoding="utf-8")


def test_reconstruction_models_have_one_writer_owner() -> None:
    constructors = {
        "PrepaidFundingReconstructionBatch(": [],
        "PrepaidFundingBaseline(": [],
    }
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/models/prepaid_funding.py":
            continue
        source = path.read_text(encoding="utf-8")
        for constructor in constructors:
            if constructor in source:
                constructors[constructor].append(relative)

    assert constructors == {
        "PrepaidFundingReconstructionBatch(": [
            "app/services/prepaid_funding_reconstruction.py"
        ],
        "PrepaidFundingBaseline(": ["app/services/prepaid_funding_reconstruction.py"],
    }


def test_runtime_has_no_legacy_authority_toggle_or_fallback() -> None:
    position = _read("app/services/customer_financial_position.py")
    owner = _read("app/services/prepaid_funding_reconstruction.py")
    ledger = _read("app/services/customer_financial_ledger.py")
    planner = _read("app/services/prepaid_enforcement_planner.py")
    planner_script = _read("scripts/one_off/plan_prepaid_balance_sweep.py")
    settings = _read("app/services/settings_spec.py")

    assert "verified_prepaid_funding_balance" in position
    assert "legacy_projection" not in position
    assert "prepaid_funding_authority" not in settings
    assert "SplynxBillingTransaction" not in owner
    assert "native_customer_financial_balances_by_currency" in owner
    assert "list_customer_financial_events" not in owner
    assert "SplynxBillingTransaction" not in ledger
    assert "include_legacy_mirror" not in ledger
    assert "_has_legacy_mirror" not in ledger
    assert "INTERNAL_MEMO_PREFIXES" not in ledger
    assert "affects_customer_position" in ledger
    assert "PrepaidFundingSnapshot" not in planner
    assert "funding_snapshot" not in planner
    assert "--funding-snapshot" not in planner_script


def test_materializer_requires_explicit_final_cutover_acknowledgement() -> None:
    script = _read("scripts/one_off/materialize_prepaid_funding_reconstruction.py")
    migration = _read_migration("prepaid_funding_reconstruction")

    assert "MATERIALIZE_VERIFIED_PREPAID_FUNDING" in script
    assert "--confirm-final-cutover" in script
    assert "authority cutover is final" in migration


def test_materializer_owner_requires_a_config_trusted_clean_replay_seal() -> None:
    exporter = _read("scripts/one_off/export_prepaid_funding_snapshot.py")
    materializer = _read(
        "scripts/one_off/materialize_prepaid_funding_reconstruction.py"
    )
    owner = _read("app/services/prepaid_funding_reconstruction.py")
    attestation = _read("app/services/prepaid_funding_attestation.py")
    settings = _read("app/services/settings_spec.py")

    assert "--signing-key-ref" in exporter
    assert "sealed_funding_payload" in exporter
    assert "is_openbao_ref" in exporter
    assert "required_balance" not in exporter
    assert "resolve_prepaid_thresholds" not in exporter
    assert "verify_prepaid_funding_manifest" in owner
    assert "reconstruction_existing_attestation_mismatch" in owner
    assert 'blocker_manifest.get("blockers") != []' in owner
    assert "apply_prepaid_funding_reconstruction" in materializer
    assert "Ed25519PublicKey" in attestation
    assert "is_openbao_ref" in attestation
    assert "prepaid_reconstruction_attestation_public_key_ref" in settings


def test_gap_adjudication_cannot_write_money_or_override_replay() -> None:
    script = _read("scripts/one_off/adjudicate_prepaid_funding_gaps.py")

    assert "SessionLocal" not in script
    assert "Payment(" not in script
    assert "--apply" not in script
    assert "financial.payments" in script
    assert "blocked_pending_owner_actions_and_independent_replay" in script
    assert "amount/date coincidence is insufficient" in script
