from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REVISION = "32eebc1a6ac05a21275ed4db6f3d1dd28514a045"
IMAGE_DIGEST = "sha256:" + "a" * 64
IMAGE_REFERENCE = f"ghcr.io/michaelayoade/dotmac_sub@{IMAGE_DIGEST}"
# The exact compact JSON FastAPI renders for `/api/v1/health/ready`.
READY_BODY = '{"status":"ready","checks":{"database":{"status":"up"}}}'
STARTING_BODY = '{"status":"not_ready","checks":{"routes":{"status":"starting"}}}'

# Everything the deploy recreates when a host declares the full stack.
FULL_SERVICES = (
    "app",
    "celery-worker",
    "celery-worker-bandwidth",
    "celery-worker-ingestion",
    "celery-worker-monitoring",
    "celery-worker-notifications-immediate",
    "celery-worker-notifications",
    "celery-worker-billing",
    "celery-worker-tr069",
    "celery-beat",
    "bandwidth-poller",
    "syslog-listener",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_deploy(
    tmp_path: Path,
    *,
    revision: str = REVISION,
    image_source_tree: str | None = None,
    health_success: bool = True,
    primary_health_success: bool = True,
    rollback_health_success: bool = True,
    rollback_recreate_success: bool = True,
    rollback_repin_failure: str | None = None,
    proxy_ready: bool = True,
    migration_lock_failures: int = 0,
    migration_hard_failure: bool = False,
    heads_before: str = "670_a",
    heads_after: str | None = None,
    heads_read_fails: bool = False,
    database_url: str = (
        "postgresql://dotmac:not-a-real-pw@db.example.test:5432/dotmac_sub"
    ),
    migration_interrupt: bool = False,
    first_deploy: bool = False,
    manifest_pins_ready: bool = True,
    github_checks_ready: bool = True,
    background_runtime_ready: bool = True,
    rollback_gate_exit_code: int = 0,
    payments_intent_rollback_gate_exit_code: int | None = None,
    declared_services: tuple[str, ...] = FULL_SERVICES,
    write_override: bool = False,
    image_selector: str = "sha-32eebc1",
    repo_digest_matches: bool = True,
    extra_env: dict[str, str] | None = None,
    deployment_target: str = "staging",
    docker_prelude: str = "",
    candidate_ready_body: str = READY_BODY,
    primary_ready_body: str = READY_BODY,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    deploy_dir = tmp_path / "deploy"
    bin_dir = tmp_path / "bin"
    deploy_dir.mkdir(exist_ok=True)
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker_log.write_text("")
    migration_attempts = tmp_path / "migration-attempts"
    migration_attempts.write_text("0")
    up_attempts = tmp_path / "up-attempts"
    up_attempts.write_text("0")
    curl_attempts = tmp_path / "curl-attempts"
    curl_attempts.write_text("0")
    sed_attempts = tmp_path / "sed-attempts"
    sed_attempts.write_text("0")
    heads_file = tmp_path / "alembic-heads"
    heads_file.write_text(heads_before + "\n")
    app_env, server_name = (
        ("production", "dotmac-sub-prod")
        if deployment_target == "production"
        else ("staging", "dotmac-sub-staging")
    )
    (deploy_dir / ".env").write_text(
        (
            ""
            if first_deploy
            else "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000\n"
            "GIT_SHA=old0000000000000000000000000000000000000\n"
        )
        + f"DATABASE_URL={database_url}\n"
        f"APP_ENV={app_env}\n"
        f"SERVER_NAME={server_name}\n"
    )
    declared_services_literal = " ".join(
        f"'{service}'" for service in declared_services
    )
    if write_override:
        (deploy_dir / "docker-compose.override.yml").write_text(
            "services:\n  app: {}\n"
        )
    reported_digest_reference = (
        IMAGE_REFERENCE
        if repo_digest_matches
        else "ghcr.io/michaelayoade/dotmac_sub@sha256:" + "b" * 64
    )
    repo_root = Path(__file__).resolve().parents[1]
    release_source_tree = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    reported_source_tree = image_source_tree or release_source_tree
    # Resolved Compose is the source of the candidate component plan. Bind
    # the image to the real deploy invocation's APP_IMAGE, including digest
    # selectors, instead of returning an empty success for the new gate.
    candidate_service_graph = json.dumps(
        {
            "services": {
                name: {
                    "image": "__candidate_image__",
                    "working_dir": "/app",
                    "environment": {"PYTHONPATH": "/app"},
                    "volumes": [],
                }
                for name in declared_services
            }
        }
    )
    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$DOCKER_LOG"
{docker_prelude}
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$*" == *"RepoDigests"* ]]; then
    printf '%s\\n' "{reported_digest_reference}"
  elif [[ "$*" == *"io.dotmac.release.source-tree"* ]]; then
    printf '%s\\n' "{reported_source_tree}"
  else
    printf '%s\\n' "{revision}"
  fi
fi
if [[ "$*" == *"alembic upgrade heads"* ]]; then
  attempts="$(cat "$MIGRATION_ATTEMPTS")"
  if ((attempts < {migration_lock_failures})); then
    printf '%s\\n' "$((attempts + 1))" > "$MIGRATION_ATTEMPTS"
    echo "canceling statement due to lock timeout" >&2
    exit 1
  fi
  if [[ "{int(migration_interrupt)}" == "1" ]]; then
    printf '%s\\n' "{heads_after if heads_after is not None else heads_before}" > "{heads_file}"
    top=""
    pid=$PPID
    while [[ -n "$pid" && "$pid" != "1" && "$pid" != "0" ]]; do
      if ps -o command= -p "$pid" | grep -q 'deploy.sh'; then top="$pid"; fi
      pid="$(ps -o ppid= -p "$pid" | tr -d ' ')"
    done
    kill -INT "$top"
    sleep 1
    exit 0
  fi
  if [[ "{int(migration_hard_failure)}" == "1" ]]; then
    printf '%s\\n' "{heads_after if heads_after is not None else heads_before}" > "{heads_file}"
    echo "psycopg2.errors.UndefinedTable: boom" >&2
    exit 1
  fi
fi
if [[ "$1" == "exec" && "$*" == *"alembic_version"* ]]; then
  if [[ "{int(heads_read_fails)}" == "1" ]]; then
    echo "canceling statement due to statement timeout" >&2
    exit 1
  fi
  cat "{heads_file}"
  exit 0
fi
if [[ "$*" == *"scripts.integrations.verify_manifest_pins"* ]]; then
  exit {0 if manifest_pins_ready else 1}
fi
if [[ "$*" == *"scripts.verify_payment_email_rollback"* ]]; then
  printf 'rollback-gate image=%s sha=%s\\n' "$APP_IMAGE" "$GIT_SHA" >> "$DOCKER_LOG"
  printf 'rollback-gate-command=%s\\n' "$*" >> "$DOCKER_LOG"
  exit {rollback_gate_exit_code}
fi
if [[ "$*" == *"scripts.verify_payments_intent_rollback"* ]]; then
  printf 'rollback-gate image=%s sha=%s\\n' "$APP_IMAGE" "$GIT_SHA" >> "$DOCKER_LOG"
  printf 'rollback-gate-command=%s\\n' "$*" >> "$DOCKER_LOG"
  exit {rollback_gate_exit_code if payments_intent_rollback_gate_exit_code is None else payments_intent_rollback_gate_exit_code}
fi
if [[ "$*" == *" up -d "* ]]; then
  attempts="$(cat "$UP_ATTEMPTS")"
  printf '%s\\n' "$((attempts + 1))" > "$UP_ATTEMPTS"
  # Worker and producer recreations add calls before rollback. Fail only the
  # recreation that uses the restored previous-image pin.
  if [[ "{int(not rollback_recreate_success)}" == "1" ]] && \\
    grep -Fxq 'APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000' "$DEPLOY_DIR/.env"; then
    exit 1
  fi
fi
if [[ "$*" == *"config --format json"* ]]; then
  config='{candidate_service_graph}'
  printf '%s\\n' "${{config//__candidate_image__/${{APP_IMAGE:?}}}}"
  exit 0
fi
if [[ "$*" == *"--entrypoint /opt/venv/bin/python"* && "$*" == *"-m scripts.setup.verify_openbao_boot_secrets"* ]]; then
  printf 'OpenBao boot-secret fixture: ready\\n'
  exit 0
fi
if [[ "$*" == *"--entrypoint /opt/venv/bin/python"* && "$*" == *"-m scripts.setup.verify_deployment_secret_requirements"* ]]; then
  printf 'Declared secret requirements satisfied.\\n'
  exit 0
fi
if [[ "$*" == *"config --services"* ]]; then
  printf '%s\\n' {declared_services_literal}
  exit 0
fi
if [[ "$*" == "compose "*" ps -q "* ]]; then
  printf 'container-%s\\n' "${{@: -1}}"
  exit 0
fi
if [[ "$1" == "inspect" && "$*" == *".State.Status"* ]]; then
  printf 'running\\n'
  exit 0
fi
if [[ "$1" == "inspect" && "$*" == *".RestartCount"* ]]; then
  printf '0\\n'
  exit 0
fi
if [[ "$1" == "inspect" && "$*" == *".Config.Hostname"* ]]; then
  printf '%s\\n' "$2"
  exit 0
fi
if [[ "$1" == "exec" && "$*" == *"scripts.verify_celery_workers"* ]]; then
  exit {0 if background_runtime_ready else 1}
fi
exit 0
""",
    )
    nginx_config = (
        "upstream dotmac_sub_app {\n  server 127.0.0.1:18002 backup;\n}"
        if proxy_ready
        else "upstream dotmac_sub_app {\n  server 127.0.0.1:8001;\n}"
    )
    _write_executable(
        bin_dir / "nginx",
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "{nginx_config}"
""",
    )
    curl_log = tmp_path / "curl.log"
    curl_log.write_text("")
    _write_executable(
        bin_dir / "curl",
        f"""#!/usr/bin/env bash
url="${{@: -1}}"
printf '%s\\n' "$url" >> "$CURL_LOG"
# Readiness documents answer per web process; liveness keeps its attempt order
# (candidate, primary, then any restored previous image).
if [[ "$url" == *"/api/v1/health/ready" ]]; then
  if [[ "$url" == *":18002/"* ]]; then
    printf '%s' '{candidate_ready_body}'
  else
    printf '%s' '{primary_ready_body}'
  fi
  exit 0
fi
attempts="$(cat "$CURL_ATTEMPTS")"
printf '%s\\n' "$((attempts + 1))" > "$CURL_ATTEMPTS"
if [[ "$attempts" == "0" ]]; then exit {0 if health_success else 1}; fi
if [[ "$attempts" == "1" ]]; then exit {0 if primary_health_success else 1}; fi
exit {0 if rollback_health_success else 1}
""",
    )
    _write_executable(
        bin_dir / "sed",
        f"""#!/usr/bin/env bash
if [[ "$*" == *"s|^GIT_SHA="* && "{rollback_repin_failure}" == "git_sha" ]]; then
  attempts="$(cat "$SED_ATTEMPTS")"
  printf '%s\\n' "$((attempts + 1))" > "$SED_ATTEMPTS"
  if [[ "$attempts" == "1" ]]; then exit 1; fi
fi
exec /usr/bin/sed "$@"
""",
    )
    _write_executable(
        bin_dir / "python3",
        f"""#!/usr/bin/env bash
set -eu
printf 'host-python cwd=%s args=%s\\n' "$PWD" "$*" >> "$DOCKER_LOG"
# Pure, stdlib-only deploy helpers run for real against the test's files.
if [[ "$*" == *"-m scripts.deploy_config_freshness"* || "$*" == *"-m scripts.deployment_secret_readiness"* ]]; then
  exec "{sys.executable}" "$@"
fi
if [[ "$*" == *"-m scripts.release_candidate_evidence verify-production"* ]]; then
  printf '%s\\n' "{revision}"
  exit 0
fi
if [[ "$*" == *"-m scripts.release_backup_policy verify-production-decision"* ]]; then
  printf '%s\\n' "skip_production_hotfix"
  exit 0
fi
if [[ "$*" == *"scripts/verify_github_release.py"* ]]; then
  if [[ "{int(github_checks_ready)}" != "1" ]]; then
    echo "GITHUB RELEASE GATE REJECTED: CI=failure" >&2
    exit 1
  fi
  exit 0
fi
exit 0
""",
    )
    _write_executable(bin_dir / "pgrep", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(bin_dir / "flock", "#!/usr/bin/env bash\nexit 0\n")

    repo_root = Path(__file__).resolve().parents[1]
    production_evidence: dict[str, str] = {}
    if deployment_target == "production":
        authorization = tmp_path / "authorization.json"
        authorization.write_text("{}")
        backup_decision = tmp_path / "backup-decision.json"
        backup_decision.write_text("{}")
        production_evidence = {
            "PRODUCTION_RELEASE_EVIDENCE": str(authorization),
            "PRODUCTION_BACKUP_DECISION_FILE": str(backup_decision),
        }
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DEPLOY_DIR": str(deploy_dir),
        "REPO_DIR": str(repo_root),
        "DEPLOY_LOCK_FILE": str(tmp_path / "deploy.lock"),
        "DEPLOY_BACKUP_MODE": "skip_staging",
        "IMAGE_RETAIN_COUNT": "5",
        "HEALTH_TIMEOUT_SECONDS": (
            "0"
            if not (
                health_success and primary_health_success and rollback_health_success
            )
            else "180"
        ),
        "CANDIDATE_HEALTH_TIMEOUT_SECONDS": (
            "0"
            if not (
                health_success and primary_health_success and rollback_health_success
            )
            else "600"
        ),
        "CANDIDATE_DRAIN_SECONDS": "0",
        "BACKGROUND_RUNTIME_TIMEOUT_SECONDS": "0",
        "BACKGROUND_STABILITY_SECONDS": "0",
        "MIGRATION_RETRY_SECONDS": "0",
        "DOCKER_LOG": str(docker_log),
        "MIGRATION_ATTEMPTS": str(migration_attempts),
        "UP_ATTEMPTS": str(up_attempts),
        "CURL_ATTEMPTS": str(curl_attempts),
        "CURL_LOG": str(curl_log),
        "SED_ATTEMPTS": str(sed_attempts),
        **production_evidence,
        **(extra_env or {}),
    }
    for key in list(env):
        if key.startswith(
            ("DATABASE_URL", "DB_BACKUP", "DB_CONTAINER")
        ) and key not in (extra_env or {}):
            env.pop(key)
    result = subprocess.run(
        ["bash", str(repo_root / "scripts/deploy.sh"), image_selector],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, deploy_dir / ".env", docker_log


def test_deploy_pins_git_sha_from_image_revision(tmp_path: Path) -> None:
    result, env_file, docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in env_text
    assert f"GIT_SHA={REVISION}" in env_text
    commands = docker_log.read_text().splitlines()
    # The shared harness must exercise the real component parser and both
    # candidate module gates, rather than globally skipping readiness.
    graph = next(
        index
        for index, command in enumerate(commands)
        if "config --format json" in command
    )
    parser = next(
        index
        for index, command in enumerate(commands)
        if "-m scripts.deployment_secret_readiness" in command
    )
    boot = next(
        index
        for index, command in enumerate(commands)
        if "-m scripts.setup.verify_openbao_boot_secrets" in command
    )
    features = [
        index
        for index, command in enumerate(commands)
        if "-m scripts.setup.verify_deployment_secret_requirements" in command
    ]
    migration = next(
        index
        for index, command in enumerate(commands)
        if "alembic upgrade heads" in command
    )
    # The config writer and parser are concurrent sides of one pipeline.
    assert max(graph, parser) < boot < features[0] < migration < features[1]
    assert "--require-installed-schema" in commands[features[1]]


def test_deploy_pins_exact_oci_digest(tmp_path: Path) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        image_selector=IMAGE_DIGEST,
    )

    assert result.returncode == 0, result.stderr
    assert f"APP_IMAGE={IMAGE_REFERENCE}" in env_file.read_text()
    commands = docker_log.read_text().splitlines()
    assert f"manifest inspect {IMAGE_REFERENCE}" in commands
    assert f"pull {IMAGE_REFERENCE}" in commands


def test_production_release_modules_run_from_the_authorized_checkout(
    tmp_path: Path,
) -> None:
    """A stale deploy directory must not shadow the authorized Actions checkout."""

    result, env_file, docker_log = _run_deploy(
        tmp_path,
        image_selector=IMAGE_DIGEST,
        deployment_target="production",
        extra_env={"PRODUCTION_DEPLOY_DEADLINE_EPOCH": ""},
    )

    # This fixture proves module selection, not trusted production admission.
    # Both release gates must run before the missing trusted deadline refuses
    # cutover; do not invent lock/maintenance FDs to make a full deploy pass.
    assert result.returncode != 0
    assert "trusted production job deadline is absent or malformed" in result.stderr
    repo_root = Path(__file__).resolve().parents[1]
    release_gate = next(
        command
        for command in docker_log.read_text().splitlines()
        if "-m scripts.release_candidate_evidence verify-production" in command
    )
    assert f"cwd={repo_root}" in release_gate
    assert f"cwd={env_file.parent}" not in release_gate
    backup_gate = next(
        command
        for command in docker_log.read_text().splitlines()
        if "-m scripts.release_backup_policy verify-production-decision" in command
    )
    assert f"cwd={repo_root}" in backup_gate
    assert f"cwd={env_file.parent}" not in backup_gate
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_file.read_text()
    assert "alembic upgrade heads" not in docker_log.read_text()


def test_deploy_rejects_digest_not_reported_by_pulled_image(tmp_path: Path) -> None:
    result, env_file, _docker_log = _run_deploy(
        tmp_path,
        image_selector=IMAGE_DIGEST,
        repo_digest_matches=False,
    )

    assert result.returncode != 0
    assert "pulled image is not pinned" in result.stderr
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )


def test_deploy_rejects_non_green_github_revision_before_database_work(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        github_checks_ready=False,
    )

    assert result.returncode != 0
    assert "GITHUB RELEASE GATE REJECTED" in result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text
    commands = docker_log.read_text().splitlines()
    gate = next(
        index
        for index, command in enumerate(commands)
        if "scripts/verify_github_release.py" in command
    )
    assert not any(
        "scripts.migration.reconcile_service_extension_duplicates" in command
        or "alembic upgrade heads" in command
        for command in commands[gate + 1 :]
    )


def test_production_rejects_generic_skip_backup_before_database_work(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        image_selector=IMAGE_DIGEST,
        deployment_target="production",
        extra_env={"SKIP_BACKUP": "1"},
    )

    assert result.returncode != 0
    assert "production does not accept SKIP_BACKUP=1" in result.stderr
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    assert not any(
        "scripts.migration.reconcile_service_extension_duplicates" in command
        for command in docker_log.read_text().splitlines()
    )


def test_deploy_rejects_tag_revision_mismatch_without_changing_env(
    tmp_path: Path,
) -> None:
    result, env_file, _docker_log = _run_deploy(tmp_path, revision="f" * 40)

    assert result.returncode != 0
    assert "IMAGE INTEGRITY FAILURE" in result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text


def test_deploy_rejects_release_compose_tree_mismatch_before_database_work(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        image_source_tree="f" * 40,
    )

    assert result.returncode != 0
    assert "release Compose tree" in result.stderr
    assert "does not match image source tree" in result.stderr
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )
    assert not any(
        "scripts.migration.reconcile_service_extension_duplicates" in command
        or "alembic upgrade heads" in command
        for command in docker_log.read_text().splitlines()
    )


def test_deploy_reports_candidate_before_health_failure_rollback(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(tmp_path, health_success=False)

    assert result.returncode != 0
    assert "Warm candidate health gate failed" in result.stderr
    assert "timeout 0s" in result.stderr
    assert "Warm candidate container state:" in result.stderr
    assert "Warm candidate logs (last 200 lines):" in result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text
    commands = docker_log.read_text().splitlines()
    candidate_command = next(
        command for command in commands if "127.0.0.1:18002:8001" in command
    )
    diagnostic_logs = commands.index("logs --tail 200 dotmac_sub_app_candidate")
    rollback_cleanup = commands.index(
        "rm -f dotmac_sub_app_candidate", diagnostic_logs + 1
    )
    rollback_gate = next(
        i
        for i, command in enumerate(commands)
        if "scripts.verify_payment_email_rollback" in command
    )
    intent_rollback_gate = next(
        i
        for i, command in enumerate(commands)
        if "scripts.verify_payments_intent_rollback" in command
    )
    assert "run --no-deps -d" in candidate_command
    assert "run --rm --no-deps -d" not in candidate_command
    assert diagnostic_logs < rollback_gate < rollback_cleanup
    assert rollback_gate < intent_rollback_gate < rollback_cleanup


def test_activated_or_unknown_floor_never_starts_old_image_after_health_failure(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path, health_success=False, rollback_gate_exit_code=2
    )

    assert result.returncode != 0
    assert "DEPLOY ROLLBACK REFUSED" in result.stderr
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in (
        env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    gate = next(
        i
        for i, item in enumerate(commands)
        if "scripts.verify_payment_email_rollback" in item
    )
    assert not any(
        "scripts.verify_payments_intent_rollback" in item for item in commands
    )
    assert (
        f"rollback-gate image=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1 sha={REVISION}"
        in commands
    )
    assert not any(" up -d " in item for item in commands[gate:])
    assert "rm -f dotmac_sub_app_candidate" not in commands[gate:]


def test_payments_intent_floor_refusal_keeps_candidate_after_email_floor_passes(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        health_success=False,
        payments_intent_rollback_gate_exit_code=2,
    )

    assert result.returncode != 0
    assert "DEPLOY ROLLBACK REFUSED" in result.stderr
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in (
        env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    email_gate = next(
        i
        for i, item in enumerate(commands)
        if "scripts.verify_payment_email_rollback" in item
    )
    intent_gate = next(
        i
        for i, item in enumerate(commands)
        if "scripts.verify_payments_intent_rollback" in item
    )
    assert email_gate < intent_gate
    assert not any(" up -d " in item for item in commands[intent_gate:])
    assert "rm -f dotmac_sub_app_candidate" not in commands[intent_gate:]


def test_paused_floor_keeps_healthy_candidate_on_worker_failure(tmp_path: Path) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path, background_runtime_ready=False, rollback_gate_exit_code=2
    )

    assert result.returncode != 0
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in (
        env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    gate = next(
        i
        for i, item in enumerate(commands)
        if "scripts.verify_payment_email_rollback" in item
    )
    assert not any(" up -d " in item for item in commands[gate:])
    assert "rm -f dotmac_sub_app_candidate" not in commands[gate:]


def test_partial_repin_failure_retains_candidate_and_reports_uncertain_pin(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        background_runtime_ready=False,
        rollback_repin_failure="git_sha",
    )

    assert result.returncode != 0
    assert "DEPLOY RESTORE FAILED: previous-image pin is incomplete" in result.stderr
    assert "inspect APP_IMAGE/GIT_SHA" in result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert f"GIT_SHA={REVISION}" in env_text
    commands = docker_log.read_text().splitlines()
    gate = next(
        i
        for i, item in enumerate(commands)
        if "scripts.verify_payment_email_rollback" in item
    )
    assert not any(" up -d " in item for item in commands[gate:])
    assert "rm -f dotmac_sub_app_candidate" not in commands[gate:]


def test_previous_image_recreate_failure_retains_candidate(tmp_path: Path) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        background_runtime_ready=False,
        rollback_recreate_success=False,
    )

    assert result.returncode != 0
    assert "DEPLOY RESTORE FAILED: previous-image recreation failed" in result.stderr
    assert "previous image restored and healthy" not in result.stderr
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    gate = next(
        i
        for i, item in enumerate(commands)
        if "scripts.verify_payment_email_rollback" in item
    )
    assert any(" up -d " in item for item in commands[gate:])
    assert "rm -f dotmac_sub_app_candidate" not in commands[gate:]


def test_previous_image_unhealthy_after_recreate_retains_candidate(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        primary_health_success=False,
        rollback_health_success=False,
    )

    assert result.returncode != 0
    assert "DEPLOY RESTORE FAILED: previous image is not healthy" in result.stderr
    assert "previous image restored and healthy" not in result.stderr
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    gate = next(
        i
        for i, item in enumerate(commands)
        if "scripts.verify_payment_email_rollback" in item
    )
    assert any(" up -d " in item for item in commands[gate:])
    assert "rm -f dotmac_sub_app_candidate" not in commands[gate:]


def test_deploy_verifies_schema_then_warms_candidate_before_recreate(
    tmp_path: Path,
) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    commands = docker_log.read_text().splitlines()
    migration = next(
        index
        for index, command in enumerate(commands)
        if "alembic upgrade heads" in command
    )
    verification = next(
        index
        for index, command in enumerate(commands)
        if "scripts.migration.verify_schema_contracts" in command
    )
    manifest_pins = next(
        index
        for index, command in enumerate(commands)
        if "scripts.integrations.verify_manifest_pins" in command
    )
    candidate = next(
        index
        for index, command in enumerate(commands)
        if "127.0.0.1:18002:8001" in command
    )
    candidate_command = commands[candidate]
    recreate = next(
        index
        for index, command in enumerate(commands)
        if " up -d --force-recreate app" in command
    )

    assert migration < verification < manifest_pins < candidate < recreate
    assert "run --no-deps -d" in candidate_command
    assert "run --rm --no-deps -d" not in candidate_command


def test_deploy_rejects_unavailable_manifest_pin_before_candidate(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        manifest_pins_ready=False,
    )

    assert result.returncode != 0
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    assert any("scripts.integrations.verify_manifest_pins" in item for item in commands)
    assert not any("127.0.0.1:18002:8001" in item for item in commands)


def test_deploy_refuses_replacement_without_proxy_handoff(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(tmp_path, proxy_ready=False)

    assert result.returncode != 0
    assert "DEPLOY AVAILABILITY FAILURE" in result.stderr
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    assert docker_log.read_text() == ""


def test_deploy_retries_a_bounded_migration_lock_timeout(tmp_path: Path) -> None:
    result, _env_file, docker_log = _run_deploy(
        tmp_path,
        migration_lock_failures=2,
    )

    assert result.returncode == 0, result.stderr
    attempts = [
        command
        for command in docker_log.read_text().splitlines()
        if "alembic upgrade heads" in command
    ]
    assert len(attempts) == 3


def _assert_failed_pre_swap_migration_left_no_new_state(
    docker_log: Path,
) -> None:
    log = docker_log.read_text()
    assert "rollback-gate" not in log
    assert "verify_payment_email_rollback" not in log
    assert "--name dotmac_sub_app_candidate" not in log
    assert " up -d " not in log


_HEADS_ENV = {"DB_BACKUP_DB_NAME": "dotmac_sub"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"migration_lock_failures": 4},  # lock-blocked on every bounded attempt
        {"migration_hard_failure": True},  # non-lock failure, heads unchanged
    ],
)
def test_failed_migration_restores_pin_without_rollback_or_recreate(
    tmp_path: Path, kwargs: dict[str, object]
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env=_HEADS_ENV,
        **kwargs,  # type: ignore[arg-type]
    )

    assert result.returncode != 0
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text
    assert "restored .env pin to" in result.stderr.lower()
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_failed_migration_that_advanced_heads_keeps_pin_and_refuses_to_guess(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env=_HEADS_ENV,
        migration_hard_failure=True,
        heads_after="670_b",
    )

    assert result.returncode != 0
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" not in (
        env_file.read_text()
    )
    assert "Do not recreate containers; repair forward" in result.stderr
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_heads_query_timeout_counts_as_unreadable_and_keeps_pin(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env=_HEADS_ENV,
        migration_hard_failure=True,
        heads_read_fails=True,
    )

    assert result.returncode != 0
    assert "sha-old0000" not in env_file.read_text()
    assert "<unreadable>" in result.stderr
    assert "Do not recreate containers; repair forward" in result.stderr
    assert "PGOPTIONS=-c lock_timeout=5s -c statement_timeout=15s" in (
        docker_log.read_text()
    )
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_heads_query_against_another_database_keeps_pin(tmp_path: Path) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env={"DB_BACKUP_DB_NAME": "some_other_db"},
        migration_hard_failure=True,
    )

    assert result.returncode != 0
    assert "sha-old0000" not in env_file.read_text()
    assert "targets database 'some_other_db' but alembic uses 'dotmac_sub'" in (
        result.stderr
    )
    assert "not-a-real-pw" not in result.stdout + result.stderr
    assert "Do not recreate containers; repair forward" in result.stderr
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_first_deploy_failed_migration_says_there_is_no_previous_pin(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env=_HEADS_ENV,
        migration_hard_failure=True,
        first_deploy=True,
    )

    assert result.returncode != 0
    assert "no previous pin and no running dotmac_sub_app container" in (result.stderr)
    assert "matches the running" not in result.stderr
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" not in (
        env_file.read_text()
    )
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_interrupt_during_migration_with_unchanged_heads_repins(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path, extra_env=_HEADS_ENV, migration_interrupt=True
    )

    assert result.returncode == 130
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )
    assert "heads measured unchanged" in result.stderr
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_interrupt_during_migration_with_advanced_heads_keeps_pin(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env=_HEADS_ENV,
        migration_interrupt=True,
        heads_after="670_b",
    )

    assert result.returncode == 130
    assert "sha-old0000" not in env_file.read_text()
    assert "Do not recreate containers; repair forward" in result.stderr
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_exported_database_url_differing_from_env_refuses_before_any_change(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env={"DATABASE_URL": "postgresql://u:s3cr3t@h.example.test/other"},
    )

    assert result.returncode != 0
    assert "DEPLOY REFUSED: DATABASE_URL is exported" in result.stderr
    assert "s3cr3t" not in result.stdout + result.stderr
    assert "sha-old0000" in env_file.read_text()
    assert "alembic upgrade heads" not in docker_log.read_text()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:s3cr3t@db.example.test",
        "postgresql://u:s3cr3t@db.example.test?password=ab/cd",
        "postgresql://u:p@s3cr3t@db.example.test",
    ],
)
def test_unparseable_database_url_does_not_leak_in_full_script(
    tmp_path: Path, url: str
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env={"DB_BACKUP_DB_NAME": "dotmac_sub"},
        migration_hard_failure=True,
        database_url=url,
    )

    assert result.returncode != 0
    assert "s3cr3t" not in result.stdout + result.stderr
    assert "database name unparseable" in result.stderr
    assert "sha-old0000" not in env_file.read_text()
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_query_string_secret_fragment_is_never_printed_on_db_mismatch(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        extra_env={"DB_BACKUP_DB_NAME": "dotmac_sub"},
        migration_hard_failure=True,
        database_url="postgresql://u@db.example.test/other?password=p@ss/word",
    )

    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "alembic uses 'other'" in result.stderr
    assert "p@ss" not in out and "ss/word" not in out and "'word'" not in out
    assert "sha-old0000" not in env_file.read_text()
    _assert_failed_pre_swap_migration_left_no_new_state(docker_log)


def test_migration_container_is_named_per_attempt_and_kept_removable(
    tmp_path: Path,
) -> None:
    result, _env_file, docker_log = _run_deploy(
        tmp_path, extra_env=_HEADS_ENV, migration_lock_failures=2
    )

    assert result.returncode == 0, result.stderr
    runs = [
        c for c in docker_log.read_text().splitlines() if "alembic upgrade heads" in c
    ]
    names = [c.split("--name ")[1].split()[0] for c in runs]
    assert len(names) == 3 and len(set(names)) == 3
    assert all(n.startswith("dotmac_sub_migrate_") for n in names)
    assert all(" --rm " in c for c in runs)


def test_deploy_verifies_every_worker_and_beat_before_success(tmp_path: Path) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    commands = docker_log.read_text().splitlines()
    ping_commands = [
        command for command in commands if "scripts.verify_celery_workers" in command
    ]
    assert len(ping_commands) == 2
    for service in (
        "celery-worker",
        "celery-worker-bandwidth",
        "celery-worker-ingestion",
        "celery-worker-monitoring",
        "celery-worker-notifications-immediate",
        "celery-worker-notifications",
        "celery-worker-billing",
        "celery-worker-tr069",
        "celery-beat",
    ):
        assert any(f"ps -q {service}" in command for command in commands)
        if service != "celery-beat":
            assert all(
                f"celery@container-{service}" in command for command in ping_commands
            )
    assert all(
        "celery@container-celery-worker-notifications-immediate=notifications_immediate"
        in command
        for command in ping_commands
    )
    assert all(
        "celery@container-celery-worker-notifications=notifications" in command
        for command in ping_commands
    )


def test_deploy_rolls_back_when_a_celery_worker_is_unavailable(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        background_runtime_ready=False,
    )

    assert result.returncode != 0
    assert "Background runtime health gate failed" in result.stderr
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )
    assert any(
        "scripts.verify_celery_workers" in command
        for command in docker_log.read_text().splitlines()
    )


def test_deploy_never_starts_a_service_the_host_does_not_declare(
    tmp_path: Path,
) -> None:
    """Staging gates celery-beat behind a profile so it cannot originate
    production work. Naming a profile-gated service on the command line
    activates it, so the deploy must recreate only declared services."""
    staging_services = tuple(
        service for service in FULL_SERVICES if service != "celery-beat"
    )
    result, _env_file, docker_log = _run_deploy(
        tmp_path,
        declared_services=staging_services,
    )

    assert result.returncode == 0, result.stderr
    recreate_commands = [
        command
        for command in docker_log.read_text().splitlines()
        if " up -d " in command
    ]
    assert recreate_commands
    assert not any("celery-beat" in command for command in recreate_commands)
    # ...and it must not be health-gated as if it were running either.
    assert "BACKGROUND RUNTIME FAILURE: celery-beat" not in result.stderr


def test_deploy_refuses_missing_required_celery_worker(tmp_path: Path) -> None:
    missing_notification_worker = tuple(
        service
        for service in FULL_SERVICES
        if service != "celery-worker-notifications-immediate"
    )
    result, _env_file, docker_log = _run_deploy(
        tmp_path,
        declared_services=missing_notification_worker,
    )

    assert result.returncode != 0
    assert "DEPLOY CONFIG FAILURE" in result.stderr
    assert "celery-worker-notifications-immediate" in result.stderr
    assert not any(
        " up -d " in command for command in docker_log.read_text().splitlines()
    )


def test_deploy_loads_the_compose_override_when_the_host_has_one(
    tmp_path: Path,
) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path, write_override=True)

    assert result.returncode == 0, result.stderr
    compose_commands = [
        command
        for command in docker_log.read_text().splitlines()
        if command.startswith("compose ")
    ]
    assert compose_commands
    repo_compose = str(Path(__file__).resolve().parents[1] / "docker-compose.yml")
    host_override = str(tmp_path / "deploy" / "docker-compose.override.yml")
    assert all(
        f"-f {repo_compose}" in command and f"-f {host_override}" in command
        for command in compose_commands
    )


def test_deploy_uses_release_compose_instead_of_host_base_file(
    tmp_path: Path,
) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    repo_compose = str(Path(__file__).resolve().parents[1] / "docker-compose.yml")
    host_compose = str(tmp_path / "deploy" / "docker-compose.yml")
    compose_commands = [
        command
        for command in docker_log.read_text().splitlines()
        if command.startswith("compose ")
    ]
    assert compose_commands
    assert all(f"-f {repo_compose}" in command for command in compose_commands)
    assert all(f"-f {host_compose}" not in command for command in compose_commands)


def test_deploy_ignores_the_compose_override_when_told_to(tmp_path: Path) -> None:
    result, _env_file, docker_log = _run_deploy(
        tmp_path,
        write_override=True,
        extra_env={"IGNORE_COMPOSE_OVERRIDE": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert not any(
        "docker-compose.override.yml" in command
        for command in docker_log.read_text().splitlines()
    )


def test_deploy_requires_the_proxy_contract_unless_explicitly_opted_out(
    tmp_path: Path,
) -> None:
    """Production must keep the warm-handoff gate; a host with no proxy in
    front of the app has to opt out deliberately."""
    result, _env_file, _docker_log = _run_deploy(
        tmp_path,
        proxy_ready=False,
        extra_env={"REQUIRE_PROXY_HANDOFF": "0"},
    )

    assert result.returncode == 0, result.stderr
    assert "DEPLOY AVAILABILITY FAILURE" not in result.stderr


def test_web_gates_wait_for_readiness_not_just_liveness(tmp_path: Path) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    urls = (tmp_path / "curl.log").read_text().splitlines()
    candidate_ready = urls.index("http://127.0.0.1:18002/api/v1/health/ready")
    primary_ready = urls.index("http://127.0.0.1:8001/api/v1/health/ready")
    # Liveness answers first, then the readiness document decides.
    assert urls.index("http://127.0.0.1:18002/health") < candidate_ready
    assert candidate_ready < urls.index("http://127.0.0.1:8001/health")
    assert urls.index("http://127.0.0.1:8001/health") < primary_ready
    assert "readiness at http://127.0.0.1:8001/api/v1/health/ready" in result.stdout
    assert " up -d --force-recreate app" in docker_log.read_text()


def test_live_but_unready_candidate_fails_closed_before_replacement(
    tmp_path: Path,
) -> None:
    """`/health` answering is not enough: a candidate still loading routes (or
    unable to reach its database) must never be handed the primary's traffic."""

    result, env_file, docker_log = _run_deploy(
        tmp_path,
        candidate_ready_body=STARTING_BODY,
        extra_env={"CANDIDATE_HEALTH_TIMEOUT_SECONDS": "0"},
    )

    assert result.returncode != 0
    assert "Warm candidate health gate failed" in result.stderr
    assert (
        "Warm candidate readiness never reported ready at "
        "http://127.0.0.1:18002/api/v1/health/ready; last answer: "
        f"{STARTING_BODY}"
    ) in result.stderr
    assert "Warm candidate logs (last 200 lines):" in result.stderr
    commands = docker_log.read_text().splitlines()
    assert not any(" up -d " in command for command in commands)
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )


def test_unready_primary_takes_the_existing_rollback_floor_path(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        primary_ready_body=STARTING_BODY,
        extra_env={"HEALTH_TIMEOUT_SECONDS": "0"},
    )

    assert result.returncode != 0
    assert "Primary app readiness never reported ready" in result.stderr
    commands = docker_log.read_text().splitlines()
    assert any("scripts.verify_payment_email_rollback" in c for c in commands)
    # The restored previous image is judged by liveness alone: it may predate
    # the readiness contract.
    urls = (tmp_path / "curl.log").read_text().splitlines()
    assert urls[-1] == "http://127.0.0.1:8001/health"
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )


def test_readiness_url_follows_the_host_health_url(tmp_path: Path) -> None:
    result, _env_file, _docker_log = _run_deploy(
        tmp_path,
        extra_env={"HEALTH_URL": "http://10.120.121.20:8001/health"},
    )

    assert result.returncode == 0, result.stderr
    urls = (tmp_path / "curl.log").read_text().splitlines()
    assert "http://10.120.121.20:8001/api/v1/health/ready" in urls
