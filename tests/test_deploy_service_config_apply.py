"""A deploy applies changed freeradius/vmagent/promtail config, safely.

These services are never recreated by ``scripts/deploy.sh`` and none of them
re-reads its config while running, so a pulled config change used to stay
unapplied until someone restarted the container by hand (dotmac_erp PR #695:
vmagent ran a two-week-old config and merged production and staging metrics).

The deploy now restarts exactly the running services whose checkout
bind-mount sources changed after the container started. FreeRADIUS is
subscriber authentication: its new config is validated with
``freeradius -XC`` before any database work and again before the restart, a
rejected config is never restarted into, and the restarted server must answer
the synthetic probe as well as it did before.

The harness is the fake-Docker deploy harness from
``tests/test_deploy_release_metadata.py``; the freshness decision itself runs
for real (``scripts/deploy_config_freshness.py``) against files in the test's
deployment directory.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from tests.test_deploy_release_metadata import FULL_SERVICES, _run_deploy

SERVICES_WITH_CONFIG = (*FULL_SERVICES, "vmagent", "promtail", "freeradius")
#: Docker reports nanosecond start times; one long before and one long after
#: the files this test writes.
BEFORE_CHANGE = "2000-01-01T00:00:00.000000000Z"
AFTER_CHANGE = "2999-01-01T00:00:00.000000000Z"
FAST_GATES = {
    "FREERADIUS_STABILITY_SECONDS": "0",
    "FREERADIUS_RESTART_TIMEOUT_SECONDS": "0",
}


def _checkout_config(deploy_dir: Path) -> dict[str, list[Path]]:
    """Write the deployment directory's config and return each service's sources."""

    freeradius = deploy_dir / "config" / "freeradius"
    (freeradius / "mods-enabled").mkdir(parents=True)
    (freeradius / "radiusd.conf").write_text("# radiusd\n")
    (freeradius / "mods-enabled" / "sql").write_text("# sql\n")
    vmagent = deploy_dir / "config" / "vmagent"
    vmagent.mkdir(parents=True)
    (vmagent / "config.yml").write_text("global: {}\n")
    promtail = deploy_dir / "config" / "promtail"
    promtail.mkdir(parents=True)
    (promtail / "promtail-config.yml").write_text("server: {}\n")
    return {
        "freeradius": [
            freeradius / "radiusd.conf",
            freeradius / "mods-enabled" / "sql",
        ],
        "vmagent": [vmagent],
        "promtail": [promtail],
    }


def _rfc3339(nanoseconds: int) -> str:
    seconds, fraction = divmod(nanoseconds, 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{fraction:09d}Z"


def _host_path_written_after_start(tmp_path: Path) -> tuple[str, Path]:
    """Return a start time after the checkout was written, and a host path
    outside the checkout that changed after that start.

    The pauses keep the filesystem's coarse timestamp clock on the right side
    of the start time.
    """

    time.sleep(0.05)
    started_at = _rfc3339(time.time_ns())
    time.sleep(0.05)
    host_path = tmp_path / "var-lib-docker-containers"
    host_path.mkdir()
    (host_path / "container.log").write_text("constantly appended\n")
    return started_at, host_path


def _docker_prelude(
    tmp_path: Path,
    *,
    sources: dict[str, list[Path]],
    started_at: dict[str, str],
    validation_exit_code: int = 0,
    probe_outcomes: tuple[str, ...] = ("accept",),
    not_running: tuple[str, ...] = (),
    freeradius_restart_count_step: int | None = None,
) -> str:
    """Fake-Docker behaviour for the three config services.

    ``probe_outcomes`` is consumed one per probe call; the last value repeats.
    ``freeradius_restart_count_step`` reports a RestartCount that starts at 7
    (history from before this restart) and grows by the step on every inspect;
    a growing count is what the unless-stopped policy produces for a server
    that keeps exiting.
    """

    probe_calls = tmp_path / "probe-calls"
    probe_calls.write_text("0")
    restart_count = tmp_path / "freeradius-restart-count"
    restart_count.write_text("7")
    crash_loop = (
        f"""if [[ "$1" == "inspect" && "$2" == "container-freeradius" && "$*" == *".RestartCount"* ]]; then
  count="$(cat "{restart_count}")"
  printf '%s\\n' "$((count + {freeradius_restart_count_step}))" > "{restart_count}"
  printf '%s\\n' "$count"
  exit 0
fi"""
        if freeradius_restart_count_step is not None
        else ""
    )
    started_cases = "\n".join(
        f'    container-{service}) printf "%s\\n" "{value}" ;;'
        for service, value in started_at.items()
    )
    mount_cases = "\n".join(
        f"    container-{service}) printf '%s\\n' "
        + " ".join(f"'{path}'" for path in paths)
        + " ;;"
        for service, paths in sources.items()
    )
    outcomes = " ".join(probe_outcomes)
    not_running_checks = "\n".join(
        f'if [[ "$*" == "compose "*" ps -q {service}" ]]; then exit 0; fi'
        for service in not_running
    )
    return f"""
{not_running_checks}
{crash_loop}
if [[ "$1" == "inspect" && "$*" == *".State.StartedAt"* ]]; then
  case "$2" in
{started_cases}
  esac
  exit 0
fi
if [[ "$1" == "inspect" && "$*" == *".Mounts"* ]]; then
  case "$2" in
{mount_cases}
  esac
  exit 0
fi
if [[ "$*" == *"run --rm --no-deps -T freeradius freeradius -XC"* ]]; then
  echo "Configuration appears to be OK? exit={validation_exit_code}"
  exit {validation_exit_code}
fi
if [[ "$*" == *" exec -T celery-worker python -c"* ]]; then
  outcomes=({outcomes})
  calls="$(cat "{probe_calls}")"
  printf '%s\\n' "$((calls + 1))" > "{probe_calls}"
  index=$((calls < ${{#outcomes[@]}} ? calls : ${{#outcomes[@]}} - 1))
  printf '%s\\n' "${{outcomes[$index]}}"
  exit 0
fi
"""


def _deploy(
    tmp_path: Path,
    *,
    started_at: dict[str, str],
    validation_exit_code: int = 0,
    probe_outcomes: tuple[str, ...] = ("accept",),
    not_running: tuple[str, ...] = (),
    promtail_host_path_changed: bool = False,
    freeradius_restart_count_step: int | None = None,
    gates: dict[str, str] | None = None,
):
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    sources = _checkout_config(deploy_dir)
    if promtail_host_path_changed:
        promtail_started, host_path = _host_path_written_after_start(tmp_path)
        started_at = {**started_at, "promtail": promtail_started}
        sources["promtail"].append(host_path)
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        declared_services=SERVICES_WITH_CONFIG,
        extra_env={**FAST_GATES, **(gates or {})},
        docker_prelude=_docker_prelude(
            tmp_path,
            sources=sources,
            started_at=started_at,
            validation_exit_code=validation_exit_code,
            probe_outcomes=probe_outcomes,
            not_running=not_running,
            freeradius_restart_count_step=freeradius_restart_count_step,
        ),
    )
    return result, env_file, docker_log.read_text().splitlines()


def _restarts(commands: list[str]) -> list[str]:
    return [
        command.rsplit(" restart ", 1)[1]
        for command in commands
        if command.startswith("compose ") and " restart " in command
    ]


def _validations(commands: list[str]) -> list[int]:
    return [
        index
        for index, command in enumerate(commands)
        if "freeradius freeradius -XC" in command
    ]


def _index(commands: list[str], needle: str) -> int:
    return next(index for index, command in enumerate(commands) if needle in command)


def test_unchanged_config_restarts_nothing(tmp_path: Path) -> None:
    result, _env, commands = _deploy(
        tmp_path,
        started_at=dict.fromkeys(("freeradius", "vmagent", "promtail"), AFTER_CHANGE),
    )

    assert result.returncode == 0, result.stderr
    assert _restarts(commands) == []
    assert _validations(commands) == []
    assert "freeradius: config unchanged since the container started" in (result.stdout)


def test_changed_agent_config_restarts_only_that_agent_after_the_release(
    tmp_path: Path,
) -> None:
    result, _env, commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": AFTER_CHANGE,
            "vmagent": BEFORE_CHANGE,
            "promtail": AFTER_CHANGE,
        },
    )

    assert result.returncode == 0, result.stderr
    assert _restarts(commands) == ["vmagent"]
    assert _validations(commands) == []
    recreate = _index(commands, " up -d app ")
    assert _index(commands, " restart vmagent") > recreate
    assert "vmagent: changed " in result.stdout


def test_host_paths_outside_the_checkout_never_trigger_a_restart(
    tmp_path: Path,
) -> None:
    """promtail tails /var/lib/docker/containers, which changes constantly.

    Only bind sources inside the deployment directory decide; here promtail's
    checkout config predates its start while a host path it mounts does not.
    """

    result, _env, commands = _deploy(
        tmp_path,
        started_at=dict.fromkeys(("freeradius", "vmagent"), AFTER_CHANGE),
        promtail_host_path_changed=True,
    )

    assert result.returncode == 0, result.stderr
    assert _restarts(commands) == []
    assert "promtail: config unchanged since the container started" in (result.stdout)


def test_stopped_service_is_not_started_by_a_config_change(tmp_path: Path) -> None:
    result, _env, commands = _deploy(
        tmp_path,
        started_at=dict.fromkeys(("freeradius", "vmagent", "promtail"), BEFORE_CHANGE),
        not_running=("promtail", "freeradius"),
    )

    assert result.returncode == 0, result.stderr
    assert _restarts(commands) == ["vmagent"]
    assert "promtail: not running; nothing to apply." in result.stdout


def test_changed_freeradius_config_is_validated_before_database_work_and_restart(
    tmp_path: Path,
) -> None:
    result, env_file, commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": BEFORE_CHANGE,
            "vmagent": AFTER_CHANGE,
            "promtail": AFTER_CHANGE,
        },
    )

    assert result.returncode == 0, result.stderr
    assert _restarts(commands) == ["freeradius"]
    preflight, before_restart = _validations(commands)
    # Fails fast: the first validation precedes every database step.
    assert preflight < _index(commands, "alembic upgrade heads")
    assert preflight < _index(commands, "scripts.migration.verify_schema_contracts")
    # And the restart itself is guarded by a fresh validation, after the
    # application release was accepted.
    restart = _index(commands, " restart freeradius")
    assert _index(commands, " up -d app ") < before_restart < restart
    assert "freeradius is running; probe accept." in result.stdout
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in (
        env_file.read_text()
    )


def test_rejected_freeradius_config_refuses_the_deploy_before_database_work(
    tmp_path: Path,
) -> None:
    result, env_file, commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": BEFORE_CHANGE,
            "vmagent": AFTER_CHANGE,
            "promtail": AFTER_CHANGE,
        },
        validation_exit_code=1,
    )

    assert result.returncode != 0
    assert "FREERADIUS CONFIG REJECTED" in result.stderr
    assert "DEPLOY REFUSED before any database work" in result.stderr
    assert _restarts(commands) == []
    assert not any("alembic upgrade heads" in command for command in commands)
    assert not any(" up -d " in command for command in commands)
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )


def test_freeradius_that_stops_answering_fails_the_deploy_without_app_rollback(
    tmp_path: Path,
) -> None:
    result, env_file, commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": BEFORE_CHANGE,
            "vmagent": AFTER_CHANGE,
            "promtail": AFTER_CHANGE,
        },
        # Accepting before the restart, silent after it.
        probe_outcomes=("accept", "timeout"),
    )

    assert result.returncode == 1
    assert _restarts(commands) == ["freeradius"]
    assert "FREERADIUS RESTART HEALTH FAILED" in result.stderr
    assert "probe=timeout (before restart: accept)" in result.stderr
    assert "FreeRADIUS configuration change was NOT applied" in result.stdout
    # A RADIUS problem never rolls back the healthy application release.
    assert not any("rollback-gate" in command for command in commands)
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in (
        env_file.read_text()
    )


def test_unconfigured_probe_proves_liveness_only(tmp_path: Path) -> None:
    result, _env, commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": BEFORE_CHANGE,
            "vmagent": AFTER_CHANGE,
            "promtail": AFTER_CHANGE,
        },
        probe_outcomes=("unconfigured",),
    )

    assert result.returncode == 0, result.stderr
    assert _restarts(commands) == ["freeradius"]
    assert "only liveness is proven" in result.stderr


def test_rejecting_probe_must_still_answer_after_the_restart(tmp_path: Path) -> None:
    """A probe user that is rejected still proves the auth path answers."""

    result, _env, _commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": BEFORE_CHANGE,
            "vmagent": AFTER_CHANGE,
            "promtail": AFTER_CHANGE,
        },
        probe_outcomes=("reject", "timeout"),
    )

    assert result.returncode == 1
    assert "probe=timeout (before restart: reject)" in result.stderr


def test_crash_looping_freeradius_fails_the_restart_gate(tmp_path: Path) -> None:
    """A RestartCount that keeps moving is not "running", whatever its value.

    The count deliberately starts above zero: history from before this
    restart must neither fail nor pass the gate on its own.
    """

    result, _env, commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": BEFORE_CHANGE,
            "vmagent": AFTER_CHANGE,
            "promtail": AFTER_CHANGE,
        },
        freeradius_restart_count_step=1,
        gates={
            "FREERADIUS_STABILITY_SECONDS": "3",
            "FREERADIUS_RESTART_TIMEOUT_SECONDS": "5",
        },
    )

    assert result.returncode == 1
    assert _restarts(commands) == ["freeradius"]
    assert "FREERADIUS RESTART HEALTH FAILED" in result.stderr
    assert "probe=not run" in result.stderr


def test_restart_count_history_does_not_block_a_stable_server(
    tmp_path: Path,
) -> None:
    """A stable server with an old non-zero RestartCount passes."""

    result, _env, commands = _deploy(
        tmp_path,
        started_at={
            "freeradius": BEFORE_CHANGE,
            "vmagent": AFTER_CHANGE,
            "promtail": AFTER_CHANGE,
        },
        freeradius_restart_count_step=0,
        gates={
            "FREERADIUS_STABILITY_SECONDS": "1",
            "FREERADIUS_RESTART_TIMEOUT_SECONDS": "10",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _restarts(commands) == ["freeradius"]
    assert "freeradius is running; probe accept." in result.stdout
