"""Unit tests for the feature-freeze classifier (scripts/ci)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "ci" / "check_feature_freeze.py"
)
_spec = importlib.util.spec_from_file_location("check_feature_freeze", _SCRIPT)
assert _spec and _spec.loader
freeze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(freeze)


def kinds(findings: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return sorted(findings)


def test_correction_to_existing_file_passes() -> None:
    changes = [("M", "app/services/billing/payments.py"), ("D", "app/services/old.py")]
    assert freeze.classify(changes, "fix(billing): stop double settlement") == []


def test_new_app_module_is_feature_shaped() -> None:
    findings = freeze.classify(
        [("A", "app/services/new_thing.py")], "fix: small cleanup"
    )
    assert findings == [("feature-exception", "app/services/new_thing.py")]


def test_feat_title_is_feature_shaped_even_without_new_files() -> None:
    findings = freeze.classify(
        [("M", "app/services/billing/tax.py")], "feat(billing): add loyalty points"
    )
    assert len(findings) == 1
    assert findings[0][0] == "feature-exception"
    assert findings[0][1].startswith("PR title:")


def test_new_migration_is_schema_shaped() -> None:
    findings = freeze.classify(
        [("A", "alembic/versions/900_add_points_table.py")], "fix: points"
    )
    assert findings == [("schema", "alembic/versions/900_add_points_table.py")]


def test_new_transitional_tool_is_flagged_before_generic_module_rule() -> None:
    findings = freeze.classify(
        [("A", "app/services/billing/opening_census_v9.py")], "chore: census"
    )
    assert findings == [
        ("transitional-tool", "app/services/billing/opening_census_v9.py")
    ]


def test_openapi_manifest_change_is_feature_shaped() -> None:
    findings = freeze.classify(
        [("M", "tests/architecture/openapi_contract_surface.json")], "fix: drift"
    )
    assert findings == [
        ("feature-exception", "tests/architecture/openapi_contract_surface.json")
    ]


def test_tests_and_docs_additions_always_pass() -> None:
    changes = [
        ("A", "tests/test_new_regression.py"),
        ("A", "docs/new_runbook.md"),
        ("A", "tests/architecture/openapi_contract_surface.json"),
    ]
    # The manifest is under tests/ but is explicitly gated above; the rest pass.
    findings = freeze.classify(changes[:2], "test: coverage")
    assert findings == []


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "EX-1",
        "kind": "feature-exception",
        "paths": ["app/services/new_thing.py"],
        "reason": "temporary composition shim",
        "starter_contract": "dotmac-billing OpeningImportV2",
        "approved_by": "michaelayoade",
    }
    base.update(overrides)
    return base


def test_approved_exception_covers_matching_path() -> None:
    assert freeze.exception_covers(
        [_entry()], "feature-exception", "app/services/new_thing.py"
    )


def test_exception_from_someone_else_does_not_cover() -> None:
    assert not freeze.exception_covers(
        [_entry(approved_by="collaborator")],
        "feature-exception",
        "app/services/new_thing.py",
    )


def test_transitional_exception_without_deletion_trigger_does_not_cover() -> None:
    entry = _entry(kind="transitional-tool", paths=["scripts/billing/x.py"])
    assert not freeze.exception_covers(
        [entry], "transitional-tool", "scripts/billing/x.py"
    )
    entry["deletion_trigger"] = "Delete at opening seal."
    assert freeze.exception_covers([entry], "transitional-tool", "scripts/billing/x.py")


def test_approved_growth_kind_validates_without_starter_contract() -> None:
    entry = _entry(kind="approved-growth", starter_contract="")
    assert freeze.validate_exceptions([entry]) == []
    bad = _entry(kind="approved-growth", approved_by="collaborator")
    assert freeze.validate_exceptions([bad])


def test_validate_exceptions_requires_starter_contract_for_features() -> None:
    problems = freeze.validate_exceptions([_entry(starter_contract="")])
    assert any("Starter contract" in p for p in problems)
    assert freeze.validate_exceptions([_entry()]) == []


@pytest.mark.parametrize("title", ["feat: x", "feat(api): x", "FEAT!: x"])
def test_feat_title_variants(title: str) -> None:
    assert freeze.classify([], title), title


def test_non_feat_titles_pass() -> None:
    for title in ["fix: x", "chore: feature flags cleanup", "docs: features"]:
        assert freeze.classify([], title) == [], title


def test_workflow_uses_base_branch_gate_and_trusted_inputs() -> None:
    wf = (
        Path(__file__).resolve().parents[1] / ".github/workflows/feature-freeze.yml"
    ).read_text(encoding="utf-8")
    assert "name: Feature Freeze Gate" in wf
    assert "origin/${BASE_REF}:scripts/ci/check_feature_freeze.py" in wf
    assert "--exceptions" in wf
    assert "python3 -I" in wf
    assert "python3 scripts/ci/check_feature_freeze.py" not in wf
    assert "github.event.pull_request.title" not in wf
    assert "run: git fetch --no-tags origin ${{ github.base_ref }}" in wf
