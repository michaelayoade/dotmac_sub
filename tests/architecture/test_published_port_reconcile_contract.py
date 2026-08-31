"""Reconciling a published port is ordered by the script, not by a runbook.

``scripts/reconcile_published_ports.sh`` is the only managed way to change an
infrastructure service's published ports. The ordering it exists to enforce is:
the environment value must be in place, and PROVEN in place, before the
container is recreated. Recreating ``postgres-local`` while ``PG_LOCAL_BIND``
is unset applies compose's loopback default and binds the replication standby
out of its own WAL stream -- a security fix becoming a replication outage.

A comment cannot enforce that. These tests pin the mechanisms that do:

* the recreate names ``VERIFIED_SERVICE``, which only the effective-compose
  gate assigns, so removing that gate makes the recreate abort under ``set -u``
  rather than run unverified;
* the env write, the effective-compose verification and the recreate appear in
  that order in the file;
* the script proves the container id actually changed, and reserves a distinct
  exit code for "nothing needed doing".
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "reconcile_published_ports.sh"
DEPLOY = ROOT / "scripts" / "deploy.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "infrastructure-reconcile.yml"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _workflow() -> dict[str, object]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _assert_v1_dispatch_cannot_acquire_production_runner(
    workflow: dict[str, object],
) -> None:
    jobs = workflow["jobs"]
    assert set(jobs) == {"production_preflight"}
    preflight = jobs["production_preflight"]
    assert preflight["runs-on"] == "ubuntu-latest"
    assert "environment" not in preflight
    assert all("uses" not in step for step in preflight["steps"])

    refusal = "\n".join(step.get("run", "") for step in preflight["steps"])
    assert "published-port reconcile v1 is disabled for production" in refusal
    assert "v2 two-plan/apply/deadman path" in refusal
    assert re.search(r"^exit 1$", refusal, flags=re.MULTILINE)
    assert all(not step.get("continue-on-error", False) for step in preflight["steps"])
    serialized = str(workflow)
    assert "self-hosted" not in serialized
    assert "dotmac-sub-production" not in serialized
    assert "scripts/reconcile_published_ports.sh" not in serialized


def test_the_reconcile_script_is_executable() -> None:
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_v1_refuses_production_before_reading_host_state() -> None:
    """Defense in depth if a caller bypasses the hosted workflow preflight."""
    script = _script()
    refusal = script.index('[[ "${ENVIRONMENT}" != "production" ]] ||')
    env_read = script.index('[[ -f "${ENV_FILE}" ]] ||')
    docker_read = script.index("command -v docker")
    lock = script.index("# --- one at a time, sharing the deploy lock")
    plan = script.index("# --- gate 1: plan")
    assert refusal < env_read < docker_read < lock < plan


def test_the_recreate_names_the_variable_only_the_verification_gate_sets() -> None:
    """The structural half of the ordering.

    ``VERIFIED_SERVICE`` is assigned exactly once, immediately after the
    effective-compose check. The recreate uses it rather than ``SERVICE``. Under
    ``set -u`` a future edit that drops the verification gate turns the recreate
    into an unbound-variable abort instead of an unverified recreate.
    """
    script = _script()
    assert "set -euo pipefail" in script
    assignments = re.findall(r"^VERIFIED_SERVICE=", script, flags=re.MULTILINE)
    assert len(assignments) == 1, (
        "VERIFIED_SERVICE must be assigned exactly once -- by the effective-"
        f"compose gate. Found {len(assignments)} assignments."
    )
    assert (
        'up -d --timeout "${RECREATE_TIMEOUT_SECONDS}" \\\n  "${VERIFIED_SERVICE}"'
        in script
    )


def test_env_is_written_and_verified_before_compose_is_asked_what_it_resolves() -> None:
    script = _script()
    wrote_env = script.index('log "Setting ${key}=${want} in ${ENV_FILE}"')
    reread_env = script.index('[[ "$(env_value "${key}")" == "${want}" ]] ||')
    verified = script.index('log "Verifying the effective compose configuration"')
    recreated = script.index('log "Recreating ${VERIFIED_SERVICE}')
    assert wrote_env < reread_env < verified < recreated, (
        "the env value must be written, re-read out of .env, and proven to be "
        "what compose actually resolves, all before any container is recreated"
    )


def test_the_effective_configuration_is_read_back_from_compose() -> None:
    """Not from .env, and not from the plan: from what compose resolves."""
    script = _script()
    assert '"${COMPOSE[@]}" config --format json' in script
    assert "compose resolves host_ip" in script


def test_a_failed_verification_restores_env_and_refuses_before_the_recreate() -> None:
    script = _script()
    restore = script.index(
        'die "the effective compose configuration does not match the plan'
    )
    recreated = script.index('log "Recreating ${VERIFIED_SERVICE}')
    assert restore < recreated
    assert "restore_env" in script


def test_the_script_proves_the_container_was_actually_replaced() -> None:
    """`up -d` that no-ops leaves the old binding in place."""
    script = _script()
    assert 'if [[ "${BEFORE_ID}" == "${AFTER_ID}" ]]; then' in script
    assert "Refusing to report success" in script


def test_doing_nothing_has_its_own_exit_code() -> None:
    """Exit 3, not 0, when the service already matches.

    Anchored on the branch that emits it -- not on the header comment that also
    names it -- so the guard cannot be satisfied by documentation alone.
    """
    script = _script()
    branch = script.index('log "ALREADY RECONCILED:')
    tail = script[branch : branch + 400]
    assert re.search(r"^\s*exit 3$", tail, re.MULTILINE), (
        "the ALREADY RECONCILED branch must exit 3, so that 'nothing was done' "
        "can never be read as 'the operation fixed it'"
    )
    assert not re.search(r"^\s*exit 0$", tail, re.MULTILINE)


def test_the_actual_listeners_are_re_read_after_the_recreate() -> None:
    script = _script()
    recreated = script.index('log "Recreating ${VERIFIED_SERVICE}')
    verified = script.index('log "Verifying actual listeners for ${VERIFIED_SERVICE}"')
    assert recreated < verified
    assert "check-listeners" in script[verified:]


def test_the_reconcile_shares_the_deploy_lock() -> None:
    """A recreate racing a deploy is the failure this avoids."""
    script = _script()
    assert "DEPLOY_LOCK_FILE:-/var/lock/dotmac_sub_deploy.lock" in script
    assert "flock -n 9" in script


def test_the_host_must_be_the_environment_it_is_reconciled_as() -> None:
    script = _script()
    assert "production:dotmac-sub-prod" in script
    assert '[[ "${HOST_ENVIRONMENT}" == "${ENVIRONMENT}" ]] ||' in script


def test_the_reconcile_never_reads_the_container_environment_block() -> None:
    """Only three fields leave `docker inspect`; the env block is not one."""
    script = _script()
    assert ".Config.Env" not in script
    assert "{{json .NetworkSettings.Ports}}" in script


# --------------------------------------------------------------------------
# why this script has to exist at all
# --------------------------------------------------------------------------


def _app_services() -> set[str]:
    """The exact service names deploy.sh recreates."""
    deploy = DEPLOY.read_text(encoding="utf-8")
    block = deploy[
        deploy.index("APP_SERVICES=(") : deploy.index("CELERY_WORKER_SERVICES=(")
    ]
    inner = block[block.index("(") + 1 : block.rindex(")")]
    return set(re.findall(r"[a-z0-9][a-z0-9-]*", inner.replace("\\\n", " ")))


def test_the_app_services_list_is_still_parseable() -> None:
    """Sensitivity: if this stops parsing, the two guards below go vacuous."""
    services = _app_services()
    assert "app" in services
    assert "celery-beat" in services
    assert len(services) >= 10, services


def test_a_deploy_still_does_not_recreate_the_infrastructure_services() -> None:
    """If this ever changes, the reconcile's reason for existing changes too.

    ``postgres-local`` being outside ``APP_SERVICES`` is exactly why merging a
    compose change to its published port did not close the exposure.
    """
    services = _app_services()
    for service in ("postgres-local", "redis-local", "nominatim", "freeradius"):
        assert service not in services, (
            f"{service} is now recreated by a deploy; revisit "
            "scripts/reconcile_published_ports.sh and its documentation"
        )


def test_every_declaration_agrees_with_deploy_about_who_recreates_it() -> None:
    """``recreated_by_deploy`` is a claim about deploy.sh. Hold it to that.

    It is load-bearing: it is how an operator knows whether a bind change rides
    the next release (syslog-listener) or waits for a reconcile (every other
    declared publisher).
    """
    import tomllib

    declaration = tomllib.loads(
        (ROOT / "deploy" / "published_ports.toml").read_text(encoding="utf-8")
    )
    services = _app_services()
    checked = set()
    for entry in declaration["publish"]:
        service = entry["service"]
        checked.add(service)
        assert entry["recreated_by_deploy"] == (service in services), (
            f"{service}: declaration says recreated_by_deploy="
            f"{entry['recreated_by_deploy']} but deploy.sh's APP_SERVICES "
            f"{'contains' if service in services else 'does not contain'} it"
        )
    assert {"syslog-listener", "postgres-local"} <= checked, (
        "the declaration must still cover both a deploy-recreated publisher and "
        "a reconcile-only one, or this guard proves nothing"
    )


# --------------------------------------------------------------------------
# the operation is requestable and recorded
# --------------------------------------------------------------------------


def test_v1_dispatch_fails_on_hosted_preflight_before_the_production_runner() -> None:
    _assert_v1_dispatch_cannot_acquire_production_runner(_workflow())


def test_dispatch_refusal_guard_detects_every_production_runner_bypass() -> None:
    """Sensitivity: each load-bearing edge must be necessary to this guard."""
    passing_preflight = deepcopy(_workflow())
    passing_preflight["jobs"]["production_preflight"]["steps"][0]["run"] = (
        "echo incorrectly-enabled\nexit 0\n"
    )

    self_hosted_preflight = deepcopy(_workflow())
    self_hosted_preflight["jobs"]["production_preflight"]["runs-on"] = [
        "self-hosted",
        "dotmac-sub-production",
    ]

    production_environment = deepcopy(_workflow())
    production_environment["jobs"]["production_preflight"]["environment"] = "production"

    unguarded_production_job = deepcopy(_workflow())
    unguarded_production_job["jobs"]["bypass"] = {
        "runs-on": ["self-hosted", "dotmac-sub-production"],
        "environment": "production",
        "steps": [{"run": "true"}],
    }

    restored_reconcile = deepcopy(_workflow())
    restored_reconcile["jobs"]["production_preflight"]["steps"][0]["run"] += (
        "\nbash scripts/reconcile_published_ports.sh\n"
    )

    unnamed_v2_gate = deepcopy(_workflow())
    unnamed_v2_gate["jobs"]["production_preflight"]["steps"][0]["run"] = (
        unnamed_v2_gate["jobs"]["production_preflight"]["steps"][0]["run"].replace(
            "v2 two-plan/apply/deadman path", "some later repair"
        )
    )

    mutations = {
        "preflight no longer fails": passing_preflight,
        "preflight itself acquires production": self_hosted_preflight,
        "preflight requests the production environment": production_environment,
        "new production job resurrects the executor": unguarded_production_job,
        "refusal job invokes the old script": restored_reconcile,
        "refusal drops the named v2 prerequisite": unnamed_v2_gate,
    }
    for name, workflow in mutations.items():
        try:
            _assert_v1_dispatch_cannot_acquire_production_runner(workflow)
        except AssertionError:
            continue
        raise AssertionError(f"dispatch refusal guard missed: {name}")


def test_the_reconcile_workflow_is_manual_only() -> None:
    """Never on push. Recreating a database on every deploy is far worse."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert list(triggers) == ["workflow_dispatch"], (
        "this operation is deliberate and requested, never automatic"
    )


def test_the_reconcile_workflow_requires_attribution() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True] if True in workflow else workflow["on"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    for required in ("service", "target_server_name", "change_reference", "reason"):
        assert inputs[required]["required"] is True, (
            f"{required} must be a required input: an unrecorded recreate of an "
            "infrastructure service is exactly the unattributable change this "
            "replaces"
        )


def test_v1_retirement_gate_is_identical_in_adr_and_runbook() -> None:
    adr = " ".join(
        (ROOT / "docs/adr/0014-declared-published-port-intent.md")
        .read_text(encoding="utf-8")
        .split()
    ).casefold()
    runbook = " ".join(
        (ROOT / "docs/runbooks/PUBLISHED_PORT_RECONCILE.md")
        .read_text(encoding="utf-8")
        .split()
    ).casefold()
    for document in (adr, runbook):
        assert "v1" in document and "disabled" in document
        assert "two distinct" in document
        assert "immediate third read-only replan" in document
        assert "evidence" in document and "authorization" in document
        assert "persistent=true" in document or "persistent systemd" in document
        assert "non-port" in document
