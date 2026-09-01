"""Structural proofs for the single-use legacy image-pin bootstrap lanes.

These assert the SHAPE of the mechanism: that the steady-state observer is
still digest-only, that the bootstrap cannot pull or build, that its plan lane
holds no production authority, and that the single-use refusal is a mechanism
rather than a comment.

Each guard that could pass vacuously carries a sensitivity proof beside it: a
deliberately planted violation that the guard must reject. A check over an
empty set, or a grep for a string that no longer exists, passes for the wrong
reason.
"""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAN_WORKFLOW = ROOT / ".github/workflows/legacy-image-pin-bootstrap-plan.yml"
APPLY_WORKFLOW = ROOT / ".github/workflows/legacy-image-pin-bootstrap-apply.yml"
ADAPTER = ROOT / "scripts/legacy_image_pin_bootstrap.sh"
OWNER = ROOT / "scripts/legacy_image_pin_bootstrap.py"
OBSERVER = ROOT / "scripts/legacy_image_pin_observer.py"
DEADMAN = ROOT / "scripts/legacy_image_pin_deadman.py"
CONTRACTS = ROOT / "scripts/legacy_image_pin_contracts.py"
DEADMAN_SERVICE = ROOT / "deploy/systemd/dotmac-legacy-image-pin-deadman@.service"
DEADMAN_TIMER = ROOT / "deploy/systemd/dotmac-legacy-image-pin-deadman@.timer"
STEADY_OBSERVER = ROOT / "scripts/published_port_plan_observer.py"
STEADY_CONTRACTS = ROOT / "scripts/published_port_contracts.py"


def _workflow(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _assert_actions_are_immutable(workflow: dict[str, object]) -> None:
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            action = step.get("uses")
            if action and not re.fullmatch(r"[^@]+@[0-9a-f]{40}", action):
                raise AssertionError(f"action {action!r} is not pinned to a commit")


def _assert_run_bodies_do_not_interpolate(workflow: dict[str, object]) -> None:
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "${{" in (step.get("run") or ""):
                raise AssertionError("a run body interpolates a workflow expression")


def _assert_plan_lane_is_read_only(workflow: dict[str, object]) -> None:
    jobs = workflow["jobs"]
    if set(jobs) != {"admit_read_only_plan", "plan"}:
        raise AssertionError("the plan workflow has unexpected jobs")
    if any("environment" in job for job in jobs.values()):
        raise AssertionError("the plan lane must hold no deployment environment")
    if workflow["permissions"] != {"contents": "read"}:
        raise AssertionError("the plan lane must be read-only")
    body = "".join(step.get("run") or "" for step in jobs["plan"]["steps"])
    if "legacy_image_pin_bootstrap.sh plan" not in body:
        raise AssertionError("the plan lane does not run the plan mode")
    for forbidden in (" apply ", "systemctl", "--force-recreate", "docker compose up"):
        if forbidden in body:
            raise AssertionError(f"the plan lane reaches {forbidden!r}")


def test_the_steady_state_observer_is_still_digest_only() -> None:
    """The bootstrap must not have become a hole in the steady-state rule."""

    source = STEADY_OBSERVER.read_text(encoding="utf-8")
    assert "target container image is not immutable and digest-pinned" in source
    assert "IMAGE_REFERENCE.fullmatch" in source
    # The steady-state contract still demands a digest from the target.
    contracts = STEADY_CONTRACTS.read_text(encoding="utf-8")
    assert "image_reference: ImageReference" in contracts
    assert 'r"^[^\\s@]+@sha256:[0-9a-f]{64}$"' in contracts


def test_a_non_target_has_nowhere_to_record_its_image() -> None:
    """The split is structural: non-target provenance is unrepresentable.

    A tolerated-but-recorded tag could later be read back and compared. A field
    that does not exist cannot be.
    """

    tree = ast.parse(STEADY_CONTRACTS.read_text(encoding="utf-8"))
    classes = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    non_target = classes["PublishedPortProjectContainerV1"]
    fields = {
        node.target.id
        for node in non_target.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert fields == {"service", "container", "container_id"}
    assert "image_reference" not in fields
    assert "image_id" not in fields

    target = classes["PublishedPortContainerObservationV2"]
    target_fields = {
        node.target.id
        for node in target.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert {"image_reference", "image_id", "listeners"} <= target_fields


def test_the_bootstrap_observer_cannot_pull_build_or_mutate() -> None:
    source = OBSERVER.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/python3 -I")
    assert 'CONFIG_PATH = Path("/etc/dotmac/legacy-image-pin-observer.json")' in source
    assert "--config" not in source
    tree = ast.parse(source)
    forbidden = {"up", "pull", "build", "restart", "stop", "rm", "exec", "start"}
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not (forbidden & literals), forbidden & literals


def test_the_digest_is_taken_from_the_running_image_never_from_the_tag() -> None:
    """The STOP condition is a mechanism, not a hope."""

    source = OBSERVER.read_text(encoding="utf-8")
    # The RepoDigests lookup is keyed by the running image ID.
    assert '"image",\n            "inspect",\n            image_id,' in source
    assert "{{json .RepoDigests}}" in source
    assert "cannot be bound to an immutable reference" in source
    assert "resolves to different bytes than the running" in source
    # And the contract refuses the mismatch independently of the observer.
    assert (
        "the desired digest does not resolve to the running image ID"
        in CONTRACTS.read_text(encoding="utf-8")
    )


def test_the_bootstrap_is_structurally_single_use() -> None:
    """Three independent refusals, on three different lanes."""

    owner = OWNER.read_text(encoding="utf-8")
    assert "def require_single_use(" in owner
    # PLAN, ADMISSION and the receipt writer each call it.
    assert owner.count("require_single_use(") >= 4
    assert "could not be parsed" in owner
    # The observer refuses before it will even look at the host.
    assert "_refuse_a_repeat(" in OBSERVER.read_text(encoding="utf-8")
    # The adapter refuses on both lanes before doing anything.
    adapter = ADAPTER.read_text(encoding="utf-8")
    assert adapter.count("require_single_use") >= 3
    # A rollback is terminal too.
    assert "_write_rollback_receipt(" in DEADMAN.read_text(encoding="utf-8")


def test_the_single_use_guard_would_notice_if_it_were_removed() -> None:
    """Sensitivity: the guard above must fail on a plausible regression."""

    owner = OWNER.read_text(encoding="utf-8")
    weakened = owner.replace("require_single_use(receipt_path)", "pass  # removed")
    assert weakened != owner
    assert weakened.count("require_single_use(") < owner.count("require_single_use(")


def test_the_rollback_retains_the_immutable_reference() -> None:
    """Reverting the pin would undo the only durable half of the bootstrap."""

    deadman = DEADMAN.read_text(encoding="utf-8")
    assert "retained_image_reference" in deadman
    assert "did not retain the immutable reference" in deadman
    assert "DIGEST_REFERENCE.fullmatch(retained)" in deadman
    # The rollback recreate is as narrow as the forward one.
    assert (
        '"--no-deps",\n        "--no-build",\n        "--pull",\n        "never",\n        "--force-recreate",'
        in deadman
    )
    assert "docker compose pull" not in deadman
    assert "scripts/deploy.sh" not in deadman


def test_the_only_mutation_is_the_declared_bind_and_the_declared_service() -> None:
    adapter = ADAPTER.read_text(encoding="utf-8")
    assert (
        'up -d --no-deps --no-build --pull never --force-recreate \\\n  "${SERVICE}"'
        in adapter
    )
    assert 'SERVICE="postgres-local"' in adapter
    assert "PG_LOCAL_BIND=0.0.0.0:" in adapter
    # FreeRADIUS is explicitly out of scope for this facility.
    assert "freeradius" not in adapter.lower()
    for forbidden in ("docker compose pull", "docker build", "scripts/deploy.sh"):
        assert forbidden not in adapter


def test_apply_orders_the_lock_prestate_replication_deadman_and_mutation() -> None:
    """Ordering is the property; a correct set of steps in a wrong order is wrong."""

    script = ADAPTER.read_text(encoding="utf-8")
    marks = [
        "flock -n 9",
        'sudo -n "${OBSERVER_BIN}" collect >"${SCRATCH}/prestate.json"',
        "probe_replication prestate",
        "run_owner verify-prestate",
        "does not resolve locally to the running image ID",
        'install -o root -g root -m 0600 "${SCRATCH}/state.json"',
        "systemctl enable --now",
        "ARMED=1",
        "PG_LOCAL_BIND=0.0.0.0:",
        "--force-recreate",
        "run_owner verify-postconditions",
        "disarm --operation",
        "run_owner write-receipt",
    ]
    previous = -1
    for mark in marks:
        found = script.index(mark, previous + 1)
        assert found > previous, mark
        previous = found


def test_every_sudo_is_noninteractive_and_the_guard_can_fail() -> None:
    pattern = re.compile(r"(?m)(?<![A-Za-z0-9_-])sudo(?!\s+-n(?:\s|$))")
    script = ADAPTER.read_text(encoding="utf-8")
    assert not pattern.search(script)
    planted = script.replace(
        "sudo -n systemctl daemon-reload", "sudo systemctl daemon-reload"
    )
    assert planted != script
    assert pattern.search(planted)


def test_plan_workflow_has_no_production_authority() -> None:
    workflow = _workflow(PLAN_WORKFLOW)
    _assert_plan_lane_is_read_only(workflow)
    _assert_actions_are_immutable(workflow)
    _assert_run_bodies_do_not_interpolate(workflow)
    checkout = next(
        step
        for step in workflow["jobs"]["plan"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["persist-credentials"] is False


def test_plan_guard_is_sensitive_to_a_planted_production_authority() -> None:
    for mutation in ("environment", "recreate"):
        workflow = deepcopy(_workflow(PLAN_WORKFLOW))
        if mutation == "environment":
            workflow["jobs"]["plan"]["environment"] = "production"
        else:
            workflow["jobs"]["plan"]["steps"].append(
                {"name": "sneak", "run": "docker compose up -d postgres-local"}
            )
        with pytest.raises(AssertionError):
            _assert_plan_lane_is_read_only(workflow)


def test_apply_requires_two_distinct_runs_and_a_production_environment() -> None:
    workflow = _workflow(APPLY_WORKFLOW)
    jobs = workflow["jobs"]
    assert set(jobs) == {"admit", "apply"}
    assert "environment" not in jobs["admit"]
    assert jobs["apply"]["environment"] == "production"
    assert jobs["apply"]["needs"] == "admit"
    _assert_actions_are_immutable(workflow)
    _assert_run_bodies_do_not_interpolate(workflow)
    raw = APPLY_WORKFLOW.read_text(encoding="utf-8")
    assert "run-id: ${{ inputs.first_plan_run_id }}" in raw
    assert "run-id: ${{ inputs.second_plan_run_id }}" in raw
    assert 'test "$FIRST_RUN" != "$SECOND_RUN"' in raw
    assert "run.run_attempt !== 1" in raw
    assert 'run.conclusion !== "success"' in raw
    # Only the apply job may reach the host adapter's apply mode.
    admit_body = "".join(step.get("run") or "" for step in jobs["admit"]["steps"])
    assert "legacy_image_pin_bootstrap.sh apply" not in admit_body


def test_apply_authority_and_plan_evidence_cannot_substitute_for_each_other() -> None:
    workflow = deepcopy(_workflow(APPLY_WORKFLOW))
    workflow["jobs"]["admit"]["environment"] = workflow["jobs"]["apply"].pop(
        "environment"
    )
    with pytest.raises(AssertionError):
        assert "environment" not in workflow["jobs"]["admit"]


def test_the_persistent_deadman_survives_runner_exit_and_reboot() -> None:
    service = DEADMAN_SERVICE.read_text(encoding="utf-8")
    timer = DEADMAN_TIMER.read_text(encoding="utf-8")
    assert "User=root" in service and "Group=root" in service
    assert "ProtectSystem=strict" in service
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    adapter = ADAPTER.read_text(encoding="utf-8")
    assert "systemctl enable --now" in adapter
    assert "trap signal_exit INT TERM HUP" in adapter
    assert "--reason postcondition-failure" in adapter
    assert "--reason signal" in adapter


def test_the_bind_knob_is_proved_rather_than_assumed() -> None:
    """The host's deployed Compose has a BARE publish; assuming the knob fails."""

    contracts = CONTRACTS.read_text(encoding="utf-8")
    assert "class LegacyImagePinBindKnobProofV1" in contracts
    # The refusal text is wrapped across source literals; grep a whole one.
    assert "the deployed Compose file does not " in contracts
    assert "hardcoded, not variable-driven" in contracts
    observer = OBSERVER.read_text(encoding="utf-8")
    assert "def _prove_bind_knob(" in observer
    # Two DIFFERENT injections: one alone would pass against a hardcoded value.
    assert '("0.0.0.0:", "wildcard_host_ip")' in observer
    assert '("127.0.0.1:", "control_host_ip")' in observer


def test_the_bootstrap_never_names_a_service_outside_its_scope() -> None:
    """FreeRADIUS gets the same facility later, with its own digest and window.

    A prose mention of that exclusion is fine and is not what this guards. What
    must not exist is an operable service name other than postgres-local, so
    the check is over STRING LITERALS in the code, not over the file text.
    """

    for path in (OWNER, OBSERVER, DEADMAN, CONTRACTS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            ast.get_docstring(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        }
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        } - docstrings
        assert not {item for item in literals if "freeradius" in item.lower()}, (
            path.name
        )
        assert not {
            item for item in literals if item in {"redis-local", "victoriametrics"}
        }, path.name
