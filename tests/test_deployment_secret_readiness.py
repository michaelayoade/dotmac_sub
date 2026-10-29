"""Readiness refuses before target mutation and never prints Compose secrets."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.deployment_secret_readiness import (
    SecretReadinessPlanError,
    SecretReadinessPlanQuery,
    candidate_components,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "registry.example.test/app@sha256:" + "a" * 64
SECRET = "synthetic-secret-must-not-appear"


def test_candidate_service_graph_ignores_secret_values() -> None:
    document = json.dumps(
        {"services": {"app": {"image": IMAGE, "environment": {"TOKEN": SECRET}}}}
    )
    result = candidate_components(document, SecretReadinessPlanQuery(IMAGE, ("app",)))
    assert result[0].name == "app"
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "components, document",
    [
        (("app",), {"services": {}}),
        (("app",), {"services": {"app": {"image": SECRET}}}),
        (("app", "app"), {"services": {"app": {"image": IMAGE}}}),
        (("app\nunsafe",), {"services": {}}),
    ],
)
def test_unproved_or_unsafe_graph_is_refused(
    components: tuple[str, ...], document: dict[str, object]
) -> None:
    with pytest.raises(SecretReadinessPlanError) as caught:
        candidate_components(
            json.dumps(document), SecretReadinessPlanQuery(IMAGE, components)
        )
    assert SECRET not in str(caught.value)


def test_parser_failure_cannot_echo_host_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{" + SECRET))
    assert main(["--image", IMAGE, "--component", "app"]) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def _run_preflight(
    tmp_path: Path,
    *,
    fail: str = "",
    beat: bool = False,
    wrong_image: bool = False,
    source_override: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    function_start = deploy.index("preflight_feature_secret_requirements() {")
    invocation_start = deploy.index(
        'log "Verifying candidate secret readiness before database work"'
    )
    invocation_end = deploy.index("\nverify_database_prerequisites", invocation_start)
    invocation_end += len("\nverify_database_prerequisites")
    contract = deploy[function_start:invocation_end]
    document = {
        "services": {
            name: {
                "image": SECRET if wrong_image and name == "app" else IMAGE,
                "environment": {"OPENBAO_TOKEN": SECRET},
            }
            for name in ("app", "celery-worker", "celery-beat")
        }
    }
    if source_override is not None:
        document["services"]["app"]["volumes"] = [
            {"type": "bind", "source": SECRET, "target": source_override}
        ]
    (tmp_path / "compose.json").write_text(json.dumps(document), encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    harness = r"""
set -euo pipefail
IMAGE="$TEST_IMAGE"
FULL_SHA=1111111111111111111111111111111111111111
CELERY_WORKER_SERVICES=(celery-worker)
CELERY_BEAT_SERVICE=celery-beat
COMPOSE=(compose)
log() { printf '%s\n' "$*"; }
service_is_declared() { [[ "$TEST_BEAT" == 1 || "$1" != celery-beat ]]; }
run_repo_module() {
  [[ "$1" == scripts.deployment_secret_readiness ]]
  shift
  "$TEST_PYTHON" "$TEST_ROOT/scripts/deployment_secret_readiness.py" "$@"
}
compose() {
  [[ "$APP_IMAGE" == "$IMAGE" && "$GIT_SHA" == "$FULL_SHA" ]]
  if [[ "$1" == config ]]; then
    echo config >> "$TEST_CALLS"
    cat compose.json
    return 0
  fi
  [[ "$1" == run && "$3" == --no-deps && "$4" == --entrypoint && "$5" == /opt/venv/bin/python ]]
  echo "$6:$8" >> "$TEST_CALLS"
  if [[ "$6" == "$TEST_FAIL" || ( "$TEST_FAIL" == features && "$8" == scripts.setup.verify_deployment_secret_requirements ) ]]; then
    echo "$TEST_SECRET" >&2
    return 1
  fi
  printf '%s: ready\n' "$6"
}
verify_rate_owner_enabled() { echo rate_owner >> "$TEST_CALLS"; }
run_database_prerequisite_bootstrap() { echo bootstrap >> "$TEST_CALLS"; }
verify_database_prerequisites() { echo prerequisites >> "$TEST_CALLS"; }
"""
    env = dict(os.environ)
    env.update(
        TEST_ROOT=str(ROOT),
        TEST_PYTHON=sys.executable,
        TEST_IMAGE=IMAGE,
        TEST_FAIL=fail,
        TEST_BEAT="1" if beat else "0",
        TEST_SECRET=SECRET,
        TEST_CALLS=str(calls),
    )
    result = subprocess.run(
        ["bash", "-c", harness + "\n" + contract],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("failure", ["app", "celery-worker", "features"])
def test_failed_probe_stops_before_any_database_bootstrap(
    tmp_path: Path, failure: str
) -> None:
    result, calls = _run_preflight(tmp_path, fail=failure)
    assert result.returncode != 0
    assert not {"rate_owner", "bootstrap", "prerequisites"} & set(calls)
    assert SECRET not in result.stdout + result.stderr


def test_override_image_mismatch_stops_before_starting_a_probe(tmp_path: Path) -> None:
    result, calls = _run_preflight(tmp_path, wrong_image=True)
    assert result.returncode != 0
    assert calls == ["config"]
    assert SECRET not in result.stdout + result.stderr


def test_declared_boot_components_pass_before_bootstrap_without_activating_beat(
    tmp_path: Path,
) -> None:
    result, calls = _run_preflight(tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "config",
        "app:scripts.setup.verify_openbao_boot_secrets",
        "celery-worker:scripts.setup.verify_openbao_boot_secrets",
        "app:scripts.setup.verify_deployment_secret_requirements",
        "rate_owner",
        "bootstrap",
        "prerequisites",
    ]
    assert SECRET not in result.stdout + result.stderr


def test_active_beat_uses_its_own_runtime_configuration(tmp_path: Path) -> None:
    result, calls = _run_preflight(tmp_path, beat=True)
    assert result.returncode == 0, result.stderr
    assert "celery-beat:scripts.setup.verify_openbao_boot_secrets" in calls
    assert calls[-3:] == ["rate_owner", "bootstrap", "prerequisites"]


@pytest.mark.parametrize(
    "target",
    [
        "/app/app",
        "/app/scripts",
        "/app/alembic",
        "/app/alembic.ini",
        "/opt/venv",
        "/opt/poetry",
        "/usr/local/lib",
        "/usr/local/bin",
        "/",
        "/app",
        "/opt",
        "/usr",
        "/usr/local",
        "/app/app/services",
        "/app/scripts/setup",
        "/opt/venv/lib/python3.12",
        "/app/uploads/../scripts",
        "/app/uploads/../../app/app",
        "//app//scripts/./setup",
        "/safe/../opt/venv",
        "/usr/local/bin/../lib",
    ],
)
def test_mount_overlap_with_candidate_code_is_refused(target: str) -> None:
    document = {
        "services": {
            "app": {
                "image": IMAGE,
                "volumes": [
                    {"type": "bind", "source": SECRET, "target": target},
                ],
            }
        }
    }
    with pytest.raises(SecretReadinessPlanError) as caught:
        candidate_components(
            json.dumps(document), SecretReadinessPlanQuery(IMAGE, ("app",))
        )
    assert SECRET not in str(caught.value)
    assert target not in str(caught.value)


@pytest.mark.parametrize(
    "volumes",
    [
        None,
        {},
        "malformed",
        ["/host:/app/scripts"],
        [None],
        [{}],
        [{"target": None}],
        [{"target": 5}],
        [{"target": ""}],
        [{"target": "app/scripts"}],
        [{"target": "/app/uploads\x00"}],
    ],
)
def test_malformed_resolved_volume_graph_is_refused(volumes: object) -> None:
    document = {"services": {"app": {"image": IMAGE, "volumes": volumes}}}
    with pytest.raises(SecretReadinessPlanError):
        candidate_components(
            json.dumps(document), SecretReadinessPlanQuery(IMAGE, ("app",))
        )


@pytest.mark.parametrize(
    "target",
    [
        "/app/uploads",
        "/etc/wireguard",
        "/var/run/docker.sock",
        "/run/secrets/openbao-token",
        "/etc/dotmac",
        "/app/config",
        "/app/uploads/../uploads",
        "/opt/venv2",
        "/usr/local/lib64",
    ],
)
def test_data_secret_and_config_mounts_remain_available(target: str) -> None:
    document = {
        "services": {
            "app": {
                "image": IMAGE,
                "volumes": [
                    {"type": "bind", "source": SECRET, "target": target},
                ],
            }
        }
    }
    assert (
        candidate_components(
            json.dumps(document), SecretReadinessPlanQuery(IMAGE, ("app",))
        )[0].name
        == "app"
    )


@pytest.mark.parametrize(
    "working_dir", [None, 5, [], "/", "/override", "/app/scripts", "/app/."]
)
def test_working_directory_override_is_refused(working_dir: object) -> None:
    document = {"services": {"app": {"image": IMAGE, "working_dir": working_dir}}}
    with pytest.raises(SecretReadinessPlanError):
        candidate_components(
            json.dumps(document), SecretReadinessPlanQuery(IMAGE, ("app",))
        )


@pytest.mark.parametrize(
    "environment",
    [
        None,
        [],
        ["PYTHONPATH=/app"],
        {"PYTHONPATH": None},
        {"PYTHONPATH": []},
        {"PYTHONPATH": "/override"},
        {"PYTHONPATH": "/app:/override"},
        {"PYTHONPATH": "."},
        {"PYTHONHOME": None},
        {"PYTHONHOME": "/opt/venv"},
        {"PYTHONHOME": "/app"},
    ],
)
def test_python_environment_override_is_refused(environment: object) -> None:
    document = {"services": {"app": {"image": IMAGE, "environment": environment}}}
    with pytest.raises(SecretReadinessPlanError):
        candidate_components(
            json.dumps(document), SecretReadinessPlanQuery(IMAGE, ("app",))
        )


@pytest.mark.parametrize("working_dir", ["", "/app"])
@pytest.mark.parametrize("pythonpath", ["", "/app"])
def test_candidate_runtime_defaults_are_allowed(
    working_dir: str, pythonpath: str
) -> None:
    document = {
        "services": {
            "app": {
                "image": IMAGE,
                "working_dir": working_dir,
                "environment": {
                    "PYTHONPATH": pythonpath,
                    "PYTHONHOME": "",
                    "OPENBAO_TOKEN": SECRET,
                },
                "volumes": [],
            }
        }
    }
    assert (
        candidate_components(
            json.dumps(document), SecretReadinessPlanQuery(IMAGE, ("app",))
        )[0].name
        == "app"
    )


@pytest.mark.parametrize(
    "source_override", ["/app", "/app/uploads/../scripts", "/opt/venv"]
)
def test_code_overlay_stops_before_any_probe_or_bootstrap(
    tmp_path: Path, source_override: str
) -> None:
    result, calls = _run_preflight(tmp_path, source_override=source_override)
    assert result.returncode != 0
    assert calls == ["config"]
    assert SECRET not in result.stdout + result.stderr
