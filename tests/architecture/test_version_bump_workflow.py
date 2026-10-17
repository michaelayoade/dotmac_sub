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


def test_open_pr_job_installs_nothing_and_runs_only_the_validator() -> None:
    text = yaml.safe_dump(_workflow()["jobs"]["open-pr"])
    for forbidden in ("poetry", "pip ", "setup-python", "scripts.", "npm"):
        assert forbidden not in text
    assert "scripts/ci/validate_bump_patch.py" in text


def test_open_pr_validates_then_applies_patch_with_pipefail() -> None:
    steps = _steps("open-pr")
    step = next(s for s in steps if s.get("name") == "Validate and apply bump patch")
    run = step["run"]
    assert "set -euo pipefail" in run
    assert (
        run.index("validate_bump_patch.py")
        < run.index("git apply --check")
        < run.index('git apply "$PATCH_FILE"')
    )
    names = [s.get("name") for s in steps]
    assert names.index("Validate and apply bump patch") < next(
        i for i, s in enumerate(steps) if "create-pull-request" in str(s.get("uses"))
    )


def test_seed_step_carries_no_token() -> None:
    step = next(s for s in _steps("bump") if s.get("name") == "Seed pending version")
    assert "TOKEN" not in yaml.safe_dump(step).upper()


def test_validator_allowlist_matches_bump_script_outputs() -> None:
    from scripts.ci.validate_bump_patch import ALLOWED_PATHS

    assert "VERSION" in ALLOWED_PATHS
    assert "deploy/product-manifest.json" in ALLOWED_PATHS
    assert "deploy/product.toml" in ALLOWED_PATHS
