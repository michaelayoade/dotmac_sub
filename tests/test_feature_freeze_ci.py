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


# --- disguised-feature rules (new template, size threshold) ---------------


def test_new_template_is_feature_shaped_even_with_fix_title() -> None:
    findings = freeze.classify_detailed(
        [("A", "templates/admin/dashboard/_sla_card.html")], "fix(ui): card"
    )
    assert findings == [
        (
            "feature-exception",
            "templates/admin/dashboard/_sla_card.html",
            "new-template",
        )
    ]


def test_modified_template_passes() -> None:
    assert (
        freeze.classify([("M", "templates/admin/dashboard/index.html")], "fix: x") == []
    )


def test_new_web_module_is_feature_shaped_under_fix_title() -> None:
    findings = freeze.classify([("A", "app/web/admin/sla.py")], "refactor: tidy")
    assert findings == [("feature-exception", "app/web/admin/sla.py")]


def test_size_threshold_flags_files_and_names_rule() -> None:
    changes = [("M", "app/services/a.py"), ("M", "templates/b.html")]
    added = {"app/services/a.py": 300, "templates/b.html": 101}
    findings = freeze.classify_detailed(changes, "fix: big", added)
    assert [(k, p) for k, p, _ in findings] == [
        ("feature-exception", "app/services/a.py"),
        ("feature-exception", "templates/b.html"),
    ]
    assert all(r.startswith("size-threshold") for _, _, r in findings)


def test_size_at_threshold_passes() -> None:
    added = {"app/services/a.py": 250, "templates/b.html": 150}
    assert freeze.classify([("M", "app/services/a.py")], "fix: x", added) == []


def test_size_ignores_tests_migrations_lockfiles_docs() -> None:
    added = {
        "tests/test_big.py": 900,
        "alembic/versions/1_x.py": 900,
        "app/static/package-lock.json": 900,
        "docs/big.md": 900,
        "app/services/a.py": 10,
    }
    assert freeze.classify([("M", "app/services/a.py")], "fix: x", added) == []


def test_size_threshold_is_configurable() -> None:
    added = {"app/services/a.py": 50}
    assert freeze.classify([], "fix: x", added, size_threshold=40)
    assert not freeze.classify([], "fix: x", added, size_threshold=60)


def test_pr_3662_shape_is_flagged_under_fix_title() -> None:
    changes = [
        ("M", "app/services/ticket_sla_reports.py"),
        ("M", "app/services/web_admin_dashboard.py"),
        ("M", "templates/admin/dashboard/index.html"),
        ("A", "tests/test_ticket_sla_reports.py"),
    ]
    added = {
        "app/services/ticket_sla_reports.py": 225,
        "app/services/web_admin_dashboard.py": 300,
        "templates/admin/dashboard/index.html": 188,
        "tests/test_ticket_sla_reports.py": 200,
    }
    findings = freeze.classify(changes, "fix(ui): ticket SLA dashboard", added)
    paths = {p for _, p in findings}
    assert "app/services/ticket_sla_reports.py" in paths
    assert "app/services/web_admin_dashboard.py" in paths
    assert "templates/admin/dashboard/index.html" in paths
    assert "tests/test_ticket_sla_reports.py" not in paths


def test_exception_entry_covers_size_finding() -> None:
    entry = _entry(paths=["app/services/a.py"])
    assert freeze.exception_covers([entry], "feature-exception", "app/services/a.py")


def test_nonascii_template_path_is_parsed_and_counted() -> None:
    path = "templates/c\u00e4rd.html"
    changes = freeze.parse_name_status_z(f"A\x00{path}\x00")
    assert changes == [("A", path)]
    added = freeze.parse_numstat_z(f"500\t0\t{path}\x00")
    assert added == {path: 500}
    rules = [r for _, _, r in freeze.classify_detailed(changes, "fix: x", added)]
    assert "new-template" in rules


def test_numstat_z_has_no_rename_form() -> None:
    # With --no-renames git emits a delete plus an add, never "{a => b}".
    out = "0\t280\tscripts/tool.py\x00280\t0\tapp/services/tool.py\x00"
    assert freeze.parse_numstat_z(out)["app/services/tool.py"] == 280


def test_diff_flags_disable_renames_and_quote() -> None:
    assert "--no-renames" in freeze._DIFF_FLAGS and "-z" in freeze._DIFF_FLAGS


def _git_repo_with_rename(tmp_path: Path, dest: str, lines: int) -> tuple[str, str]:
    import subprocess

    def g(*a: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *a],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    g("init", "-q", "-b", "main")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/tool.py").write_text("x = 1\n" * lines)
    g("add", "-A")
    g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD").strip()
    (tmp_path / dest).parent.mkdir(parents=True, exist_ok=True)
    g("mv", "scripts/tool.py", dest)
    g("commit", "-qm", "move")
    return base, g("rev-parse", "HEAD").strip()


def test_rename_into_app_services_counts_and_is_new_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head = _git_repo_with_rename(tmp_path, "app/services/tool.py", 450)
    monkeypatch.chdir(tmp_path)
    changes = freeze._name_status(base, head)
    added = freeze._numstat(base, head)
    assert ("A", "app/services/tool.py") in changes
    assert added["app/services/tool.py"] == 450
    rules = {
        r.split(":")[0]
        for _, _, r in freeze.classify_detailed(changes, "fix: x", added)
    }
    assert {"new-python-module"} <= rules


def test_rename_into_templates_is_new_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head = _git_repo_with_rename(tmp_path, "templates/tool.html", 10)
    monkeypatch.chdir(tmp_path)
    changes = freeze._name_status(base, head)
    rules = [r for _, _, r in freeze.classify_detailed(changes, "fix: x")]
    assert "new-template" in rules


def test_uncountable_diff_is_a_finding() -> None:
    added = freeze.parse_numstat_z("-\t-\tapp/services/hidden.py\x00")
    findings = freeze.classify_detailed(
        [("M", "app/services/hidden.py")], "fix: x", added
    )
    assert [(k, p) for k, p, _ in findings] == [
        ("feature-exception", "app/services/hidden.py")
    ]
    assert findings[0][2].startswith("uncountable-diff")


def test_approved_growth_covers_template_and_size_but_not_modules() -> None:
    entry = _entry(
        kind="approved-growth", paths=["templates/x.html", "app/services/a.py"]
    )
    for rule in ("new-template", "size-threshold: big"):
        assert freeze.exception_covers(
            [entry], "feature-exception", "templates/x.html", rule
        )
    assert not freeze.exception_covers(
        [entry], "feature-exception", "app/services/a.py", "new-python-module"
    )
    assert not freeze.exception_covers(
        [entry], "feature-exception", "app/services/a.py", "uncountable-diff"
    )
    assert not freeze.exception_covers([entry], "feature-exception", "templates/x.html")
