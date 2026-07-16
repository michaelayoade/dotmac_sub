"""Guard the final prepaid funding authority boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


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
    settings = _read("app/services/settings_spec.py")

    assert "verified_prepaid_funding_balance" in position
    assert "legacy_projection" not in position
    assert "prepaid_funding_authority" not in settings
    assert "SplynxBillingTransaction" not in owner
    assert "include_legacy_mirror=False" in owner
    assert 'event.id.startswith("splynx:")' not in owner


def test_materializer_requires_explicit_final_cutover_acknowledgement() -> None:
    script = _read("scripts/one_off/materialize_prepaid_funding_reconstruction.py")
    migration = _read("alembic/versions/320_prepaid_funding_reconstruction.py")

    assert "MATERIALIZE_VERIFIED_PREPAID_FUNDING" in script
    assert "--confirm-final-cutover" in script
    assert "authority cutover is final" in migration
