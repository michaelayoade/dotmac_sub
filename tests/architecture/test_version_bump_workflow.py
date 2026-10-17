"""The version-bump workflow keeps dependency code away from write tokens."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/version-bump-pr.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["steps"]


def test_bump_job_has_no_write_permissions_or_pr_token() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    bump = workflow["jobs"]["bump"]
    assert bump["permissions"] == {"contents": "read"}
    assert "VERSION_BUMP_TOKEN" not in yaml.safe_dump(bump)
    assert "create-pull-request" not in yaml.safe_dump(bump)


def test_open_pr_job_is_the_only_writer() -> None:
    open_pr = _workflow()["jobs"]["open-pr"]
    assert open_pr["needs"] == "bump"
    assert open_pr["permissions"] == {"contents": "write", "pull-requests": "write"}


def test_all_checkouts_do_not_persist_credentials() -> None:
    for job in ("bump", "open-pr"):
        checkouts = [
            s
            for s in _steps(job)
            if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        assert checkouts, job
        for step in checkouts:
            assert step["with"]["persist-credentials"] is False


def test_open_pr_job_runs_no_dependency_installs_or_repo_code() -> None:
    text = yaml.safe_dump(_workflow()["jobs"]["open-pr"])
    for forbidden in ("poetry", "pip ", "setup-python", "scripts.", "python"):
        assert forbidden not in text


def test_open_pr_applies_patch_only_through_an_explicit_path_allowlist() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for path in (
        "VERSION",
        "deploy/product-manifest.json",
        "deploy/product.toml",
        "pyproject.toml",
        "CHANGELOG.md",
    ):
        assert path in text
    assert "ALLOWED=" in text
    assert "git apply --check" in text
    assert "git apply --summary" in text
