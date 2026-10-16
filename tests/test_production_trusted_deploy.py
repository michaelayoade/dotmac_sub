from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_trusted_deploy as trusted_deploy
from scripts import production_trusted_rollback_preflight as preflight
from scripts.release_artifact_contract import (
    GitCommitSha,
    OCIImageDigest,
    WorkflowRunId,
)


def _inputs(tmp_path: Path) -> trusted_deploy.TrustedDeploymentInput:
    deploy_directory = tmp_path / "deploy"
    deploy_directory.mkdir()
    (deploy_directory / ".env").write_text("APP_ENV=production\n")
    preflight_input = preflight.TrustedPreflightInput(
        production_authorization_path=tmp_path / "authorization.json",
        authorization_run_id=WorkflowRunId(44),
        authorization_main_revision=GitCommitSha("a" * 40),
        target_revision=GitCommitSha("b" * 40),
        target_image_digest=OCIImageDigest("sha256:" + "c" * 64),
        deploy_directory=deploy_directory,
        rollback_authorization_path=None,
        bootstrap_authorization_path=None,
        control_repository=tmp_path,
    )
    return trusted_deploy.TrustedDeploymentInput(
        preflight=preflight_input,
        staged_repository=tmp_path,
        options=trusted_deploy.DeploymentOptions(),
    )


def test_preflight_refusal_never_invokes_staged_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "trusted.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    invoked: list[bool] = []
    monkeypatch.setattr(trusted_deploy, "_CANONICAL_LOCK", lock_path)
    monkeypatch.setattr(trusted_deploy, "_require_trusted_control", lambda *_: None)
    monkeypatch.setattr(
        trusted_deploy, "_require_clean_target", lambda *_: (tmp_path, tmp_path)
    )
    monkeypatch.setattr(
        trusted_deploy,
        "_acquire_canonical_lock",
        lambda: lock_fd,
    )
    monkeypatch.setattr(
        trusted_deploy,
        "run_preflight",
        lambda *_: (_ for _ in ()).throw(
            preflight.PreflightRefused(
                preflight.PreflightRefusal.IMAGE_IDENTITY_UNPROVED
            )
        ),
    )
    monkeypatch.setattr(
        trusted_deploy,
        "_wait_for_child",
        lambda *_args, **_kwargs: invoked.append(True) or 0,
    )

    with pytest.raises(preflight.PreflightRefused):
        trusted_deploy.run_trusted_deployment(_inputs(tmp_path))
    assert invoked == []


def test_maintenance_context_uses_deployment_run_id_not_authorization_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOY_RUN_ID", "77")

    assert trusted_deploy._deployment_run_id_from_environment() == 77


def test_repair_context_keeps_authorization_and_deployment_run_ids_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    record = SimpleNamespace(
        phase=trusted_deploy.MigrationPhase.FORWARD_REPAIR_REQUIRED,
        terminal=SimpleNamespace(oom_killed=False, restart_count=0),
        identity=SimpleNamespace(
            target_revision="d" * 40,
            candidate_digest=inputs.preflight.target_image_digest.value,
            invocation_id="1e8e4220-55e5-49c2-b456-1ab889d7cffa",
        ),
    )
    monkeypatch.setenv("DEPLOY_RUN_ID", "77")
    monkeypatch.setattr(trusted_deploy, "_verify_authorization", lambda *_: None)
    monkeypatch.setattr(
        trusted_deploy, "_load_migration_journal", lambda *_: (record, "e" * 64)
    )
    monkeypatch.setattr(
        trusted_deploy,
        "_classify_transition",
        lambda *_args, **_kwargs: trusted_deploy.SourceTransition.FORWARD,
    )
    monkeypatch.setattr(
        trusted_deploy.deploy_probe,
        "_candidate",
        lambda *_args, **_kwargs: (
            "sha256:" + "f" * 64,
            inputs.preflight.target_image_digest.value,
            {},
        ),
    )
    captured: list[object] = []
    monkeypatch.setattr(
        trusted_deploy,
        "prepare_maintenance_context",
        lambda context: captured.append(context),
    )

    context, transition = trusted_deploy._maintenance_repair_context(inputs)

    assert transition is trusted_deploy.SourceTransition.FORWARD
    assert context.authorization_run_id == 44
    assert context.deployment_run_id == 77
    assert captured == [context]


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", " 77", "77\n"])
def test_maintenance_context_refuses_malformed_deployment_run_id(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOY_RUN_ID", value)

    with pytest.raises(
        trusted_deploy.TrustedDeploymentRefusal,
        match="deployment_run_id_unproved",
    ):
        trusted_deploy._deployment_run_id_from_environment()


def test_unresolved_migration_interlock_never_invokes_staged_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "trusted.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    invoked: list[bool] = []
    monkeypatch.setattr(trusted_deploy, "_acquire_canonical_lock", lambda: lock_fd)
    monkeypatch.setattr(trusted_deploy, "_require_trusted_control", lambda *_: None)
    monkeypatch.setattr(
        trusted_deploy, "_require_clean_target", lambda *_: (tmp_path, tmp_path)
    )
    monkeypatch.setattr(
        trusted_deploy,
        "guard_migration_interlock",
        lambda *_: (_ for _ in ()).throw(
            trusted_deploy.MigrationRefused("migration_interlock_pending")
        ),
    )
    monkeypatch.setattr(
        trusted_deploy,
        "_wait_for_child",
        lambda *_args, **_kwargs: invoked.append(True) or 0,
    )

    with pytest.raises(trusted_deploy.MigrationRefused):
        trusted_deploy.run_trusted_deployment(_inputs(tmp_path))
    assert invoked == []


@pytest.mark.parametrize(
    ("diff_code", "untracked_paths"),
    [(1, b""), (0, b"scripts/release_candidate_evidence.py\0")],
)
def test_target_source_changes_and_untracked_import_shadows_refuse(
    diff_code: int,
    untracked_paths: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    replies = iter(
        [
            subprocess.CompletedProcess([], diff_code, b"", b""),
            subprocess.CompletedProcess([], 0, untracked_paths, b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
        ]
    )
    monkeypatch.setattr(
        trusted_deploy.subprocess, "run", lambda *_a, **_kw: next(replies)
    )

    with pytest.raises(
        trusted_deploy.TrustedDeploymentRefusal, match="staged_source_unproved"
    ):
        trusted_deploy._require_clean_source_tree(
            tmp_path, refusal="staged_source_unproved"
        )


def test_ignored_python_cache_shadow_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replies = iter(
        [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess(
                [], 0, b"scripts/__pycache__/release_control.cpython-312.pyc\0", b""
            ),
            subprocess.CompletedProcess([], 0, b"", b""),
        ]
    )
    monkeypatch.setattr(
        trusted_deploy.subprocess, "run", lambda *_a, **_kw: next(replies)
    )

    with pytest.raises(
        trusted_deploy.TrustedDeploymentRefusal, match="staged_source_unproved"
    ):
        trusted_deploy._require_clean_source_tree(
            tmp_path, refusal="staged_source_unproved"
        )


def test_untracked_executable_source_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "scripts" / "untracked-helper"
    source.parent.mkdir()
    source.write_text("#!/bin/sh\nexit 0\n")
    source.chmod(0o755)
    replies = iter(
        [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"scripts/untracked-helper\0", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
        ]
    )
    monkeypatch.setattr(
        trusted_deploy.subprocess, "run", lambda *_a, **_kw: next(replies)
    )

    with pytest.raises(
        trusted_deploy.TrustedDeploymentRefusal, match="staged_source_unproved"
    ):
        trusted_deploy._require_clean_source_tree(
            tmp_path, refusal="staged_source_unproved"
        )


def test_workflow_special_mode_vectors_match_trusted_supervisor_parser() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/production-deploy.yml"
    ).read_text()
    assert (
        '--hotfix-change-reference "$CHANGE_REFERENCE" --hotfix-reason "$HOTFIX_REASON"'
        in workflow
    )
    assert (
        '--resume-failed-run-id "$FAILED_DEPLOYMENT_RUN_ID" --resume-backup-path "$PRIOR_BACKUP_PATH"'
        in workflow
    )
    assert "--hotfix-no-migrations" not in workflow
    assert "--resume-after-migration --failed-run-id" not in workflow
    deploy_job = workflow.split("\n  deploy:\n", maxsplit=1)[1]
    prep_step = deploy_job.split(
        "- name: Prepare typed transition authorization with trusted control",
        maxsplit=1,
    )[1].split("\n      - name:", maxsplit=1)[0]
    assert (
        'PYTHONDONTWRITEBYTECODE: "1"'
        in deploy_job.split("\n    steps:\n", maxsplit=1)[0]
    )
    assert (
        "python -m scripts.release_candidate_evidence write-rollback-authorization"
        in prep_step
    )
    assert (
        "python -m scripts.release_candidate_evidence write-bootstrap-authorization"
        in prep_step
    )
    parsed = trusted_deploy._parser().parse_args(
        [
            "--production-authorization",
            "/srv/authorization.json",
            "--authorization-run-id",
            "44",
            "--authorization-main-revision",
            "a" * 40,
            "--target-revision",
            "b" * 40,
            "--target-image-digest",
            "sha256:" + "c" * 64,
            "--deploy-directory",
            "/srv/deploy",
            "--staged-repository",
            "/srv/staged",
            "--control-repository",
            "/srv/control",
            "--hotfix-change-reference",
            "INC-44",
            "--hotfix-reason",
            "bounded maintenance",
            "--resume-failed-run-id",
            "43",
            "--resume-backup-path",
            "/srv/backup.dump",
        ]
    )
    assert parsed.hotfix_change_reference == "INC-44"
    assert parsed.resume_failed_run_id == 43


def test_canonical_lock_is_inherited_until_target_recovery_finishes(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "trusted.lock"
    marker = tmp_path / "lock-observation.txt"
    repository = Path(__file__).resolve().parents[1]
    isolated_code = r"""
import fcntl, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from scripts import production_trusted_deploy as controller
lock_path, marker = sys.argv[1], sys.argv[2]
descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
os.dup2(descriptor, 8, inheritable=True)
fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
reader = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR)\n"
    "try:\n"
    "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "    state = 'free'\n"
    "except BlockingIOError:\n"
    "    state = 'held'\n"
    "with open(sys.argv[2], 'w') as stream: stream.write(state)\n"
)
status = controller._wait_for_child(
    [sys.executable, "-c", reader, lock_path, marker],
    environment=os.environ.copy(),
    cwd=Path(lock_path).parent,
)
if status != 0 or Path(marker).read_text() != "held":
    raise SystemExit(1)
fcntl.flock(8, fcntl.LOCK_UN)
contender = os.open(lock_path, os.O_RDWR)
fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.close(contender)
os.close(descriptor)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            isolated_code,
            str(lock_path),
            str(marker),
            str(repository),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
