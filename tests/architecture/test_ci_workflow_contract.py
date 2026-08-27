"""Execute and constrain the CI workflow's change-classification contract.

Two properties of `.github/workflows/ci.yml` are load-bearing and neither is
visible from Python alone, because the decision is made by a shell script
embedded in YAML:

1. classification narrows PULL REQUESTS ONLY -- every other event runs the
   complete matrix regardless of what changed; and
2. the required aggregate contexts still REPORT when a pull request legitimately
   skips PostgreSQL, because a required check that never reports deadlocks the
   merge queue rather than failing it.

So this module extracts the real script out of the workflow and runs it, rather
than restating its logic in Python -- a reimplementation would pass while the
shipped script did something else entirely.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/ci.yml"

#: Aggregate jobs whose GitHub contexts are required for merge. Each must report
#: on every run, including one where the PostgreSQL lane is skipped.
REQUIRED_AGGREGATE_JOBS = ("integration-run", "integration-test", "test")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return _workflow()


@pytest.fixture(scope="module")
def classify_script(workflow: dict[str, Any]) -> str:
    steps = workflow["jobs"]["changes"]["steps"]
    matching = [step for step in steps if step.get("id") == "classify"]
    assert len(matching) == 1, "expected exactly one classify step in the changes job"
    script = matching[0]["run"]
    assert "postgresql-required" in script, "extracted the wrong step"
    return script


def _run_classifier(
    script: str,
    tmp_path: Path,
    *,
    event_name: str,
    head_ref: str = "",
    changed: dict[str, str] | None = None,
    base_override: str | None = None,
) -> dict[str, str]:
    """Run the workflow's own classify script against a real two-commit repo."""

    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ci@example.test")
    git("config", "user.name", "ci")
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base_sha = git("rev-parse", "HEAD")

    for name, content in (changed or {"docs/x.md": "changed\n"}).items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "change")

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    github_output = tmp_path / "github-output"
    github_output.touch()

    # The workflow calls bare `python`; give it one without depending on the
    # workstation's PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)

    base = base_sha if base_override is None else base_override
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(REPOSITORY_ROOT),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(github_output),
        "EVENT_NAME": event_name,
        "HEAD_REF": head_ref,
        "BEFORE_SHA": "",
        "PR_BASE_SHA": base if event_name == "pull_request" else "",
        "MERGE_BASE_SHA": "",
    }
    environment.pop("GITHUB_STEP_SUMMARY", None)

    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"classify script failed for {event_name}:\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return outputs


# --------------------------------------------------------------------------
# Every non-pull-request event runs the complete matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_name", ["push", "schedule", "merge_group", "workflow_dispatch"]
)
def test_non_pull_request_events_force_the_complete_matrix(
    classify_script: str, tmp_path: Path, event_name: str
) -> None:
    """A docs-only diff must NOT narrow anything outside a pull request.

    The change set here is the one most likely to be narrowed -- a single
    markdown file. If any of these events ever classified it, a branch tip
    could land, ship or be promoted on evidence a pull request chose.
    """

    outputs = _run_classifier(
        classify_script,
        tmp_path,
        event_name=event_name,
        changed={"docs/only.md": "docs\n"},
    )
    assert outputs["docs-only"] == "false"
    assert outputs["postgresql-required"] == "true"


def test_a_pull_request_is_the_only_event_that_narrows(
    classify_script: str, tmp_path: Path
) -> None:
    """Sensitivity guard for the test above: the same diff DOES narrow a PR.

    Without this, the complete-matrix assertions would keep passing if
    classification stopped working altogether.
    """

    outputs = _run_classifier(
        classify_script,
        tmp_path,
        event_name="pull_request",
        changed={"docs/only.md": "docs\n"},
    )
    assert outputs["docs-only"] == "true"
    assert outputs["postgresql-required"] == "false"


@pytest.mark.parametrize("head_ref", ["integration/wave-5", "consolidate/billing"])
def test_batch_branch_pull_requests_run_the_complete_matrix(
    classify_script: str, tmp_path: Path, head_ref: str
) -> None:
    """Batch branches carry a whole train's migrations; never narrow them."""

    outputs = _run_classifier(
        classify_script,
        tmp_path,
        event_name="pull_request",
        head_ref=head_ref,
        changed={"docs/only.md": "docs\n"},
    )
    assert outputs["docs-only"] == "false"
    assert outputs["postgresql-required"] == "true"


@pytest.mark.parametrize(
    ("changed", "docs_only", "postgresql"),
    [
        ({"docs/a.md": "x\n"}, "true", "false"),
        ({"app/services/billing.py": "x = 1\n"}, "false", "true"),
        ({"tests/staff_identity_fixtures.py": "x = 1\n"}, "false", "true"),
        ({"tests/architecture/test_x.py": "x = 1\n"}, "false", "false"),
        ({"templates/admin/x.html": "<p></p>\n"}, "false", "false"),
        ({"scripts/ci/x.py": "x = 1\n"}, "false", "true"),
        ({"docs/a.md": "x\n", "app/b.py": "x = 1\n"}, "false", "true"),
    ],
)
def test_pull_request_classification_end_to_end(
    classify_script: str,
    tmp_path: Path,
    changed: dict[str, str],
    docs_only: str,
    postgresql: str,
) -> None:
    outputs = _run_classifier(
        classify_script, tmp_path, event_name="pull_request", changed=changed
    )
    assert outputs["docs-only"] == docs_only
    assert outputs["postgresql-required"] == postgresql


@pytest.mark.parametrize(
    "base_override",
    ["", "0000000000000000000000000000000000000000"],
)
def test_a_pull_request_with_no_usable_base_runs_everything(
    classify_script: str, tmp_path: Path, base_override: str
) -> None:
    outputs = _run_classifier(
        classify_script,
        tmp_path,
        event_name="pull_request",
        changed={"docs/only.md": "docs\n"},
        base_override=base_override,
    )
    assert outputs["docs-only"] == "false"
    assert outputs["postgresql-required"] == "true"


# --------------------------------------------------------------------------
# Required contexts must still report when PostgreSQL is skipped
# --------------------------------------------------------------------------


@pytest.mark.parametrize("job_name", REQUIRED_AGGREGATE_JOBS)
def test_required_aggregate_jobs_report_even_when_the_lane_is_skipped(
    workflow: dict[str, Any], job_name: str
) -> None:
    """A required check that never reports deadlocks the merge queue.

    `always()` is what makes the job run when its `needs` were skipped, and the
    job condition must not itself require `postgresql-required` -- that would
    turn "PostgreSQL not needed" into a context that never arrives.
    """

    job = workflow["jobs"][job_name]
    condition = str(job["if"])
    assert condition.startswith("always()"), (
        f"{job_name} must run even when its dependencies are skipped, "
        f"got if: {condition!r}"
    )
    assert "postgresql-required" not in condition, (
        f"{job_name} gates its whole existence on postgresql-required, so the "
        "required context disappears instead of reporting when the lane is "
        f"legitimately skipped; got if: {condition!r}"
    )


def test_the_postgresql_shards_are_the_only_thing_classification_switches_off(
    workflow: dict[str, Any],
) -> None:
    """Scope proof: `postgresql-required` may gate the shards and nothing else."""

    gated = sorted(
        name
        for name, job in workflow["jobs"].items()
        if "postgresql-required" in str(job.get("if", ""))
    )
    assert gated == ["integration-shards"], (
        "classification is only allowed to switch off the PostgreSQL shards; "
        f"it now also gates {sorted(set(gated) - {'integration-shards'})}"
    )


def test_the_workflow_declares_a_scheduled_complete_run(
    workflow: dict[str, Any],
) -> None:
    """Without a cadence run, a classifier defect can hide indefinitely."""

    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "schedule" in triggers, (
        "ci.yml has no scheduled run, so nothing periodically executes the "
        "complete matrix independently of what a change happened to touch"
    )
    assert triggers["schedule"], "the schedule trigger declares no cron entry"


def test_the_changes_job_exposes_no_unconsumed_output(
    workflow: dict[str, Any],
) -> None:
    """An output nobody reads is a decision nobody can see being made."""

    declared = set(workflow["jobs"]["changes"]["outputs"])
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    unconsumed = {
        name for name in declared if body.count(f"needs.changes.outputs.{name}") == 0
    }
    assert not unconsumed, f"changes declares outputs no job consumes: {unconsumed}"
