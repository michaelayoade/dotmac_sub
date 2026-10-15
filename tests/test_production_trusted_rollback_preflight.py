from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_trusted_rollback_preflight as preflight
from scripts.release_artifact_contract import (
    GitCommitSha,
    OCIImageDigest,
    WorkflowRunId,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNING_REVISION = GitCommitSha("a" * 40)
TARGET_REVISION = GitCommitSha("b" * 40)
IMAGE_ID = "sha256:" + "c" * 64
CONTAINER_ID = "d" * 64
IMAGE_DIGEST = "sha256:" + "e" * 64
RUNNING_IMAGE_DIGEST = "sha256:" + "f" * 64


def _inputs(
    *,
    rollback: Path | None = None,
    bootstrap: Path | None = None,
    target_revision: GitCommitSha = TARGET_REVISION,
    target_digest: str = IMAGE_DIGEST,
) -> preflight.TrustedPreflightInput:
    return preflight.TrustedPreflightInput(
        production_authorization_path=Path("authorization.json"),
        authorization_run_id=WorkflowRunId(303),
        authorization_main_revision=GitCommitSha("f" * 40),
        target_revision=target_revision,
        target_image_digest=OCIImageDigest(target_digest),
        deploy_directory=ROOT,
        rollback_authorization_path=rollback,
        bootstrap_authorization_path=bootstrap,
        control_repository=ROOT,
    )


def _running_image(
    *,
    revision: GitCommitSha = RUNNING_REVISION,
    digest: str = RUNNING_IMAGE_DIGEST,
) -> preflight.RunningImageObservation:
    return preflight.RunningImageObservation(
        container_id=preflight.DockerContainerId(CONTAINER_ID),
        image_id=preflight.DockerImageId(IMAGE_ID),
        image_digest=OCIImageDigest(digest),
        revision=revision,
    )


def test_old_staged_wrapper_is_not_reached_before_trusted_gate() -> None:
    workflow = (ROOT / ".github/workflows/production-deploy.yml").read_text()
    control_checkout = workflow.index("- name: Checkout trusted production control")
    gate = workflow.index('python "$control/scripts/production_trusted_deploy.py"')
    staged_checkout = workflow.index("- name: Checkout authorized staged release")
    direct_invocation = "bash scripts/deploy_production.sh"
    deploy_job = workflow.index("  deploy:")

    assert (
        "ref: ${{ needs.verify.outputs.authorization_main_revision }}"
        in workflow[control_checkout:]
    )
    assert control_checkout < staged_checkout < gate
    assert 'PYTHONDONTWRITEBYTECODE: "1"' in workflow[deploy_job:gate]
    assert direct_invocation not in workflow
    assert '--control-repository "$control"' in workflow
    assert '--staged-repository "$staged"' in workflow


@pytest.mark.parametrize(
    "transition", [preflight.SourceTransition.SAME, preflight.SourceTransition.FORWARD]
)
def test_same_or_forward_transition_preserves_typed_auth_without_legacy_floor(
    transition: preflight.SourceTransition, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        preflight, "_verify_authorization", lambda _inputs: calls.append("auth")
    )
    monkeypatch.setattr(
        preflight,
        "_inspect_running_image",
        lambda: _running_image(
            revision=TARGET_REVISION
            if transition is preflight.SourceTransition.SAME
            else RUNNING_REVISION,
            digest=IMAGE_DIGEST
            if transition is preflight.SourceTransition.SAME
            else RUNNING_IMAGE_DIGEST,
        ),
    )
    monkeypatch.setattr(
        preflight, "_classify_transition", lambda *_args, **_kwargs: transition
    )
    monkeypatch.setattr(
        preflight,
        "_verify_payments_floor",
        lambda _observation, **_kwargs: pytest.fail(
            "same/forward transition must not require legacy floor"
        ),
    )

    outcome = preflight.run_preflight(_inputs(target_digest=IMAGE_DIGEST))

    assert calls == ["auth"]
    assert outcome.source_transition is transition
    assert not outcome.payments_floor_proved


def test_transition_authorization_cannot_waive_payments_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        preflight, "_verify_authorization", lambda _inputs: calls.append("production")
    )
    monkeypatch.setattr(preflight, "_inspect_running_image", _running_image)
    monkeypatch.setattr(
        preflight,
        "_classify_transition",
        lambda *_args, **_kwargs: preflight.SourceTransition.BACKWARD,
    )
    monkeypatch.setattr(
        preflight,
        "_verify_transition_authorization",
        lambda *_args, **_kwargs: calls.append("transition"),
    )
    monkeypatch.setattr(
        preflight,
        "_verify_payments_floor",
        lambda _observation, **_kwargs: (_ for _ in ()).throw(
            ValueError(preflight.PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
        ),
    )

    with pytest.raises(ValueError, match="payments_floor_refused"):
        preflight.run_preflight(_inputs(rollback=Path("rollback.json")))
    assert calls == ["production", "transition"]


@pytest.mark.parametrize(
    ("running_is_ancestor", "target_is_ancestor", "expected"),
    [
        (0, 1, preflight.SourceTransition.FORWARD),
        (1, 0, preflight.SourceTransition.BACKWARD),
        (1, 1, preflight.SourceTransition.DIVERGENT),
    ],
)
def test_classifies_exact_git_transition(
    running_is_ancestor: int,
    target_is_ancestor: int,
    expected: preflight.SourceTransition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter((running_is_ancestor, target_is_ancestor))
    monkeypatch.setattr(
        preflight,
        "_run",
        lambda _command, **_kwargs: SimpleNamespace(
            returncode=next(statuses), stdout=""
        ),
    )

    assert (
        preflight._classify_transition(
            RUNNING_REVISION, TARGET_REVISION, repository=ROOT
        )
        is expected
    )


def test_missing_or_forged_image_digest_proof_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = {
        "Id": CONTAINER_ID,
        "Name": "/dotmac_sub_app",
        "Image": IMAGE_ID,
        "State": {"Running": True},
        "Mounts": [],
        "Config": {
            "Image": f"ghcr.io/michaelayoade/dotmac_sub@{IMAGE_DIGEST}",
            "WorkingDir": "/app",
            "Env": ["PYTHONPATH=/app"],
            "Labels": {preflight._REVISION_LABEL: RUNNING_REVISION.value},
        },
    }
    image = {
        "Id": IMAGE_ID,
        "RepoDigests": ["ghcr.io/michaelayoade/dotmac_sub@sha256:" + "0" * 64],
        "Config": {"Labels": {preflight._REVISION_LABEL: RUNNING_REVISION.value}},
    }
    replies = iter(
        [
            (0, "dotmac_sub_app\n"),
            (0, json.dumps(container)),
            (0, json.dumps(image)),
        ]
    )

    def fake_run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        returncode, stdout = next(replies)
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(preflight, "_run", fake_run)

    with pytest.raises(ValueError, match="image_identity_unproved"):
        preflight._inspect_running_image()


def test_running_primary_upload_mount_does_not_block_immutable_image_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = {
        "Id": CONTAINER_ID,
        "Name": "/dotmac_sub_app",
        "Image": IMAGE_ID,
        "State": {"Running": True},
        "Mounts": [{"Destination": "/app/uploads", "Type": "bind"}],
        "Config": {
            "Image": f"ghcr.io/michaelayoade/dotmac_sub@{RUNNING_IMAGE_DIGEST}",
            "WorkingDir": "/app",
            "Env": ["PYTHONPATH=/app"],
            "Labels": {preflight._REVISION_LABEL: RUNNING_REVISION.value},
        },
    }
    image = {
        "Id": IMAGE_ID,
        "RepoDigests": [f"ghcr.io/michaelayoade/dotmac_sub@{RUNNING_IMAGE_DIGEST}"],
        "Config": {"Labels": {preflight._REVISION_LABEL: RUNNING_REVISION.value}},
    }
    replies = iter(
        [
            (0, "dotmac_sub_app\n"),
            (0, json.dumps(container)),
            (0, json.dumps(image)),
        ]
    )

    def fake_run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        returncode, stdout = next(replies)
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(preflight, "_run", fake_run)

    observation = preflight._inspect_running_image()

    assert observation is not None
    assert observation.image_digest.value == RUNNING_IMAGE_DIGEST
    calls: list[str] = []
    monkeypatch.setattr(preflight, "_inspect_running_image", lambda: observation)
    monkeypatch.setattr(
        preflight,
        "_verify_authorization",
        lambda _inputs: calls.append("authorization"),
    )
    monkeypatch.setattr(
        preflight,
        "_classify_transition",
        lambda *_args, **_kwargs: preflight.SourceTransition.SAME,
    )
    monkeypatch.setattr(
        preflight,
        "_verify_payments_floor",
        lambda *_args, **_kwargs: pytest.fail("same transition needs no legacy floor"),
    )
    outcome = preflight.run_preflight(
        _inputs(target_revision=RUNNING_REVISION, target_digest=RUNNING_IMAGE_DIGEST)
    )
    assert calls == ["authorization"]
    assert outcome.source_transition is preflight.SourceTransition.SAME


def test_floor_reader_executes_in_exact_inspected_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url = "postgresql+psycopg://runtime:secret@postgres:5432/app"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text(f"DATABASE_URL={database_url}\n", encoding="utf-8")
    observation = _running_image()
    running_container = {
        "Id": CONTAINER_ID,
        "Name": "/dotmac_sub_app",
        "Image": IMAGE_ID,
        "State": {"Running": True},
        "Mounts": [],
        "Config": {
            "Image": f"ghcr.io/michaelayoade/dotmac_sub@{RUNNING_IMAGE_DIGEST}",
            "WorkingDir": "/app",
            "Env": ["PYTHONPATH=/app", f"DATABASE_URL={database_url}"],
            "Labels": {preflight._REVISION_LABEL: RUNNING_REVISION.value},
        },
    }
    commands: list[list[str]] = []
    inputs: list[str | None] = []
    probe_states = iter(
        [
            {"Status": "created", "Running": False, "ExitCode": 0, "OOMKilled": False},
            {"Running": False, "ExitCode": 0, "OOMKilled": False},
        ]
    )

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        inputs.append(kwargs.get("input_text"))
        if command[:2] == ["docker", "inspect"] and command[-1] == CONTAINER_ID:
            return SimpleNamespace(returncode=0, stdout=json.dumps(running_container))
        if command[:2] == ["docker", "inspect"] and command[-1].startswith(
            "dotmac-sub-floor-probe-"
        ):
            probe = {
                "Name": f"/{command[-1]}",
                "Image": IMAGE_ID,
                "State": next(probe_states),
                "Mounts": [],
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "NetworkMode": f"container:{CONTAINER_ID}",
                    "Privileged": False,
                    "PidMode": "",
                    "CapAdd": [],
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Binds": [],
                    "VolumesFrom": [],
                },
                "Config": {
                    "Env": [f"DATABASE_URL={database_url}"],
                    "WorkingDir": "/app",
                    "Entrypoint": ["/opt/venv/bin/python"],
                    "Cmd": ["-I", "-B", "-c", preflight._FLOOR_PROBE_CODE],
                    "Labels": {},
                },
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(probe))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(preflight, "_run", fake_run)

    preflight._verify_payments_floor(observation, deploy_directory=tmp_path)

    create_index = next(
        i for i, command in enumerate(commands) if command[:2] == ["docker", "create"]
    )
    assert (
        commands[create_index][commands[create_index].index("--network") + 1]
        == f"container:{CONTAINER_ID}"
    )
    assert commands[create_index][-5:] == [
        IMAGE_ID,
        "-I",
        "-B",
        "-c",
        preflight._FLOOR_PROBE_CODE,
    ]
    assert database_url not in " ".join(commands[create_index])
    assert inputs[create_index] == f"DATABASE_URL={database_url}\n"
    assert commands[-1][:3] == ["docker", "rm", "--force"]


def test_floor_probe_create_failure_attempts_cleanup_before_refusing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observation = _running_image()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        preflight, "_inspect_container_by_id", lambda _container_id: observation
    )
    monkeypatch.setattr(
        preflight, "_runtime_database_url", lambda _observation: "postgresql://db/app"
    )
    monkeypatch.setattr(
        preflight,
        "_effective_database_url",
        lambda _deploy_directory: "postgresql://db/app",
    )

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[:2] == ["docker", "create"]:
            raise preflight.PreflightRefused(
                preflight.PreflightRefusal.CONTROL_COMMAND_FAILED
            )
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(preflight, "_run", fake_run)

    with pytest.raises(preflight.PreflightRefused):
        preflight._verify_payments_floor(observation, deploy_directory=tmp_path)

    assert [command[:2] for command in commands] == [
        ["docker", "create"],
        ["docker", "rm"],
    ]


def test_renamed_container_id_no_longer_certifies_primary_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = {
        "Id": CONTAINER_ID,
        "Name": "/renamed-incumbent",
        "Image": IMAGE_ID,
        "State": {"Running": True},
        "Mounts": [],
        "Config": {
            "Image": f"ghcr.io/michaelayoade/dotmac_sub@{RUNNING_IMAGE_DIGEST}",
            "WorkingDir": "/app",
            "Env": ["PYTHONPATH=/app"],
            "Labels": {preflight._REVISION_LABEL: RUNNING_REVISION.value},
        },
    }
    monkeypatch.setattr(
        preflight,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(container)
        ),
    )

    with pytest.raises(ValueError, match="image_identity_unproved"):
        preflight._inspect_container_by_id(preflight.DockerContainerId(CONTAINER_ID))
