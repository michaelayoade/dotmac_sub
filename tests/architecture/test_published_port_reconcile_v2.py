"""Structural safety proofs for the separate v2 plan/apply/deadman lanes."""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAN_WORKFLOW = ROOT / ".github/workflows/infrastructure-reconcile-plan.yml"
APPLY_WORKFLOW = ROOT / ".github/workflows/infrastructure-reconcile-apply.yml"
ADAPTER = ROOT / "scripts/reconcile_published_ports_v2.sh"
DEADMAN = ROOT / "scripts/published_port_deadman.py"
DEADMAN_SERVICE = ROOT / "deploy/systemd/dotmac-published-port-deadman@.service"
DEADMAN_TIMER = ROOT / "deploy/systemd/dotmac-published-port-deadman@.timer"


def _workflow(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _jobs(workflow: dict[str, object]) -> dict[str, object]:
    return workflow["jobs"]


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    return job["steps"]


def _assert_every_action_is_immutable(workflow: dict[str, object]) -> None:
    for job in _jobs(workflow).values():
        for step in _steps(job):
            action = step.get("uses")
            if action is not None:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action


def _assert_run_bodies_do_not_interpolate_expressions(
    workflow: dict[str, object],
) -> None:
    for job in _jobs(workflow).values():
        for step in _steps(job):
            assert "${{" not in str(step.get("run", ""))


def _assert_self_hosted_checkout_drops_credentials(
    workflow: dict[str, object], job_name: str
) -> None:
    checkout = next(
        step
        for step in _steps(_jobs(workflow)[job_name])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["persist-credentials"] is False


def _assert_every_sudo_is_noninteractive(script: str) -> None:
    assert not re.search(r"(?m)(?<![A-Za-z0-9_-])sudo(?!\s+-n(?:\s|$))", script)


def _assert_plan_workflow_is_read_only(workflow: dict[str, object]) -> None:
    jobs = _jobs(workflow)
    assert set(jobs) == {"admit_read_only_plan", "plan"}
    assert jobs["plan"]["needs"] == "admit_read_only_plan"
    assert jobs["plan"]["runs-on"][-1] == "dotmac-sub-production-plan"
    assert all("environment" not in job for job in jobs.values())
    assert workflow["permissions"] == {"contents": "read"}
    text = str(workflow)
    assert "secrets." not in text
    assert "permissions: write" not in text
    plan_commands = "\n".join(str(step.get("run", "")) for step in _steps(jobs["plan"]))
    assert "reconcile_published_ports_v2.sh plan" in plan_commands
    for forbidden in (
        " apply ",
        "apply-env",
        "systemctl",
        "sudo ",
        "docker compose up",
        "--force-recreate",
    ):
        assert forbidden not in plan_commands


def _assert_apply_authority_cannot_replace_plan_evidence(
    workflow: dict[str, object],
) -> None:
    jobs = _jobs(workflow)
    assert set(jobs) == {"admit", "apply"}

    # The hosted job observes plan evidence but has no production authority.
    admit = jobs["admit"]
    assert "environment" not in admit
    admit_text = str(admit)
    assert admit_text.count("${{ inputs.first_plan_run_id }}") >= 2
    assert admit_text.count("${{ inputs.second_plan_run_id }}") >= 2
    assert "run.run_attempt !== 1" in admit_text
    assert 'run.status !== "completed"' in admit_text
    assert 'run.conclusion !== "success"' in admit_text
    assert "admit-apply" not in admit_text

    # The production environment authorizes a later job, but that job cannot
    # act without the admitted two-run evidence produced by the hosted job.
    apply = jobs["apply"]
    assert apply["needs"] == "admit"
    assert apply["environment"] == "production"
    apply_text = str(apply)
    assert "published-port-apply-inputs-v2-${{ github.run_id }}" in apply_text
    assert "admit-apply" in apply_text
    assert "--first-dir" in apply_text
    assert "--second-dir" in apply_text


def test_plan_workflow_has_no_production_authority_or_write_path() -> None:
    workflow = _workflow(PLAN_WORKFLOW)
    _assert_plan_workflow_is_read_only(workflow)
    _assert_every_action_is_immutable(workflow)
    _assert_run_bodies_do_not_interpolate_expressions(workflow)
    _assert_self_hosted_checkout_drops_credentials(workflow, "plan")


def test_plan_guard_is_sensitive_to_an_added_environment_secret_or_apply() -> None:
    workflow = _workflow(PLAN_WORKFLOW)
    for mutation in ("environment", "secret", "apply"):
        planted = deepcopy(workflow)
        plan = _jobs(planted)["plan"]
        if mutation == "environment":
            plan["environment"] = "production"
        elif mutation == "secret":
            _steps(plan)[0]["env"] = {"TOKEN": "${{ secrets.TOKEN }}"}
        else:
            _steps(plan).append({"run": "docker compose up -d app"})
        with pytest.raises(AssertionError):
            _assert_plan_workflow_is_read_only(planted)


def test_workflow_shell_and_checkout_guards_are_sensitive() -> None:
    for path, job_name in ((PLAN_WORKFLOW, "plan"), (APPLY_WORKFLOW, "apply")):
        workflow = _workflow(path)
        expression = deepcopy(workflow)
        _steps(_jobs(expression)[job_name]).append(
            {"run": 'echo "${{ inputs.reason }}"'}
        )
        with pytest.raises(AssertionError):
            _assert_run_bodies_do_not_interpolate_expressions(expression)

        credentials = deepcopy(workflow)
        checkout = next(
            step
            for step in _steps(_jobs(credentials)[job_name])
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        checkout["with"]["persist-credentials"] = True
        with pytest.raises(AssertionError):
            _assert_self_hosted_checkout_drops_credentials(credentials, job_name)


def test_plan_branch_exits_before_every_host_mutation() -> None:
    script = ADAPTER.read_text(encoding="utf-8")
    start = script.index('if [[ "${MODE}" == "plan" ]]')
    finish = script.index("\nfi", script.index("  exit 0", start))
    plan_branch = script[start:finish]
    assert "  exit 0" in plan_branch
    assert plan_branch.count("sudo -n") == 1
    assert (
        'sudo -n "${PLAN_OBSERVER_BIN}" collect --service "${SERVICE}"' in plan_branch
    )
    assert "[[ ! -w /var/run/docker.sock ]]" in plan_branch
    assert "OBSERVER_STAT=\"$(stat -c '%u:%a'" in plan_branch
    assert "installed PLAN observer differs from reviewed source" in plan_branch
    assert 'exec 9<"${LOCK_FILE}"' in plan_branch
    for forbidden in ("systemctl", "apply-env", "--force-recreate", "STATE_ROOT"):
        assert forbidden not in plan_branch


def test_plan_observer_is_fixed_isolated_and_has_no_mutating_docker_verb() -> None:
    observer = (ROOT / "scripts/published_port_plan_observer.py").read_text(
        encoding="utf-8"
    )
    assert observer.startswith("#!/usr/bin/python3 -I\n")
    assert (
        'CONFIG_PATH = Path("/etc/dotmac/published-port-plan-observer.json")'
        in observer
    )
    assert 'commands.add_parser("collect")' in observer
    assert "--config" not in observer
    assert "--output" not in observer
    assert '"ps", "-q"' in observer
    assert '"config", "--format", "json"' in observer
    assert 'str(config["docker_bin"]),\n            "inspect"' in observer
    string_literals = {
        node.value
        for node in ast.walk(ast.parse(observer))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for verb in ("up", "pull", "build", "restart", "stop", "rm", "exec"):
        assert verb not in string_literals


def test_apply_has_separate_authenticated_authority_and_exact_two_runs() -> None:
    workflow = _workflow(APPLY_WORKFLOW)
    _assert_apply_authority_cannot_replace_plan_evidence(workflow)
    jobs = _jobs(workflow)
    assert set(jobs) == {"admit", "apply"}
    assert jobs["apply"]["needs"] == "admit"
    assert jobs["apply"]["environment"] == "production"
    assert "environment" not in jobs["admit"]
    _assert_every_action_is_immutable(workflow)
    _assert_run_bodies_do_not_interpolate_expressions(workflow)
    _assert_self_hosted_checkout_drops_credentials(workflow, "apply")
    text = APPLY_WORKFLOW.read_text(encoding="utf-8")
    assert text.count("run-id: ${{ inputs.first_plan_run_id }}") == 1
    assert text.count("run-id: ${{ inputs.second_plan_run_id }}") == 1
    assert 'test "$FIRST_RUN" != "$SECOND_RUN"' in text
    assert "run.run_attempt !== 1" in text
    assert 'run.status !== "completed"' in text
    assert 'run.conclusion !== "success"' in text
    assert 'run.head_branch !== "main"' in text
    assert "source_sha is not the current protected main head" in text
    assert "source_sha ceased to be current protected main" in text
    assert "APPLY workflow reruns are not admissible" in text
    assert "Build typed admission under the authenticated production gate" in text


def test_apply_verifies_root_owned_pydantic_toolchain_before_admission() -> None:
    workflow = _workflow(APPLY_WORKFLOW)
    steps = _steps(_jobs(workflow)["apply"])
    names = [step["name"] for step in steps]
    toolchain = names.index("Verify pinned Python and Pydantic toolchain")
    admission = names.index(
        "Build typed admission under the authenticated production gate"
    )
    assert toolchain < admission
    assert "verify-toolchain" in steps[toolchain]["run"]
    adapter = ADAPTER.read_text(encoding="utf-8")
    assert "selected Python does not provide Pydantic v2" in adapter
    assert "import pydantic, pydantic_core" in adapter
    assert "PYTHONNOUSERSITE=1" in adapter
    assert "PYTHONPATH=" in adapter
    assert '"${label} is not root-owned"' in adapter
    assert '"${label} is group/world writable"' in adapter
    assert '"${dependency_stat}" "Pydantic dependency"' in adapter


def test_every_sudo_is_noninteractive_and_the_guard_can_fail() -> None:
    adapter = ADAPTER.read_text(encoding="utf-8")
    _assert_every_sudo_is_noninteractive(adapter)
    planted = adapter.replace("sudo -n install -d", "sudo install -d", 1)
    with pytest.raises(AssertionError):
        _assert_every_sudo_is_noninteractive(planted)


def test_apply_authority_and_plan_evidence_cannot_substitute_for_each_other() -> None:
    workflow = _workflow(APPLY_WORKFLOW)

    authority_in_the_evidence_job = deepcopy(workflow)
    jobs = _jobs(authority_in_the_evidence_job)
    jobs["admit"]["environment"] = jobs["apply"].pop("environment")
    with pytest.raises(AssertionError):
        _assert_apply_authority_cannot_replace_plan_evidence(
            authority_in_the_evidence_job
        )

    authority_without_evidence = deepcopy(workflow)
    jobs = _jobs(authority_without_evidence)
    jobs["apply"].pop("needs")
    with pytest.raises((AssertionError, KeyError)):
        _assert_apply_authority_cannot_replace_plan_evidence(authority_without_evidence)


def test_apply_orders_third_plan_deadman_mutation_and_disarm() -> None:
    script = ADAPTER.read_text(encoding="utf-8")
    lock = script.index('flock -n 9 || die "another deploy')
    immediate = script.index("# Immediate third plan", lock)
    third_verified = script.index("verify-third-plan", immediate)
    state_installed = script.index('"${STATE_DIR}/state.json"', third_verified)
    timer_enabled = script.index("systemctl enable --now", state_installed)
    timer_verified = script.index("systemctl is-active --quiet", timer_enabled)
    armed = script.index("ARMED=1", timer_verified)
    env_mutation = script.index("run_owner apply-env", armed)
    recreate = script.index('"${APPLY_COMPOSE[@]}" up -d', env_mutation)
    postconditions = script.index("verify-postconditions", recreate)
    disarm = script.index('"${DEADMAN_BIN}" disarm', postconditions)
    assert (
        lock
        < immediate
        < third_verified
        < state_installed
        < timer_enabled
        < timer_verified
        < armed
        < env_mutation
        < recreate
        < postconditions
        < disarm
    )


def test_apply_and_rollback_can_recreate_exactly_one_target_without_resolution() -> (
    None
):
    adapter = ADAPTER.read_text(encoding="utf-8")
    deadman = DEADMAN.read_text(encoding="utf-8")
    exact = "up -d --no-deps --no-build --pull never --force-recreate"
    assert exact in adapter
    for flag in (
        "--no-deps",
        "--no-build",
        '"--pull",\n            "never"',
        "--force-recreate",
    ):
        assert flag in deadman
    assert "docker compose pull" not in adapter + deadman
    assert "docker compose build" not in adapter + deadman
    assert "scripts/deploy.sh" not in adapter + deadman
    assert '"${SERVICE}"' in adapter[adapter.index(exact) : adapter.index(exact) + 180]
    assert 'str(document["service"])' in deadman


def test_persistent_root_deadman_survives_runner_exit_and_reboot() -> None:
    service = DEADMAN_SERVICE.read_text(encoding="utf-8")
    timer = DEADMAN_TIMER.read_text(encoding="utf-8")
    adapter = ADAPTER.read_text(encoding="utf-8")
    assert "User=root" in service
    assert "Group=root" in service
    assert "ProtectSystem=strict" in service
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert "systemctl enable --now" in adapter
    assert 'install -o root -g root -m 0600 "${SCRATCH}/state.json"' in adapter
    assert 'install -o root -g root -m 0600 "${RELEASE_COMPOSE_FILE}"' in adapter
    assert "trap signal_exit INT TERM HUP" in adapter
    assert "--reason postcondition-failure" in adapter
    assert "--reason signal" in adapter


def test_success_requires_non_port_target_and_external_proofs_before_disarm() -> None:
    owner = (ROOT / "scripts/published_port_reconcile_v2.py").read_text(
        encoding="utf-8"
    )
    assert "one or more non-target container identities changed" in owner
    assert "target non-port service definition changed" in owner
    assert "target container did not reuse the exact running image ID" in owner
    assert "address-family listener proof is not exact" in (
        ROOT / "scripts/published_port_contracts.py"
    ).read_text(encoding="utf-8")
    assert "proofs must exactly cover every obligation" in owner
    assert "unadmitted verifier" in owner
    assert "unadmitted collector" in owner


def test_current_declaration_preserves_the_named_client_contracts() -> None:
    declaration = (ROOT / "deploy/published_ports.toml").read_text(encoding="utf-8")
    assert 'service = "postgres-local"' in declaration
    assert "host_port = 9001" in declaration
    assert "container_port = 5432" in declaration
    assert 'required_clients = ["75.119.157.91/32"]' in declaration
    assert 'service = "freeradius"' in declaration
    for port in (1812, 1813, 1822, 1823):
        assert f"host_port = {port}" in declaration
    assert 'required_clients = ["160.119.127.0/24", "102.220.189.0/24"]' in declaration
