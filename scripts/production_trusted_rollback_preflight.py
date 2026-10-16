"""Trusted-workflow gate before production executes staged release code."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from scripts.release_artifact_contract import (
    GitCommitSha,
    OCIImageDigest,
    ProductionServerName,
    WorkflowRunId,
)
from scripts.release_candidate_evidence import (
    read_bootstrap_authorization,
    read_production_authorization,
    read_rollback_authorization,
    verify_bootstrap_authorization,
    verify_production_authorization,
    verify_rollback_authorization,
)

_APP_CONTAINER: Final = "dotmac_sub_app"
_IMAGE_REPOSITORY: Final = "ghcr.io/michaelayoade/dotmac_sub"
_REVISION_LABEL: Final = "org.opencontainers.image.revision"
_REVISION_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


class SourceTransition(StrEnum):
    SAME = "same"
    FORWARD = "forward"
    BACKWARD = "backward"
    DIVERGENT = "divergent"


class PreflightRefusal(StrEnum):
    AUTHORIZATION_UNPROVED = "authorization_unproved"
    CONTAINER_INVENTORY_UNREADABLE = "container_inventory_unreadable"
    CONTAINER_INVENTORY_AMBIGUOUS = "container_inventory_ambiguous"
    IMAGE_IDENTITY_UNPROVED = "image_identity_unproved"
    SOURCE_RELATION_UNPROVED = "source_relation_unproved"
    TRANSITION_AUTHORIZATION_REQUIRED = "transition_authorization_required"
    TRANSITION_AUTHORIZATION_MISMATCH = "transition_authorization_mismatch"
    PAYMENTS_FLOOR_REFUSED = "payments_floor_refused"
    BOOTSTRAP_AUTHORIZATION_REQUIRED = "bootstrap_authorization_required"
    BOOTSTRAP_AUTHORIZATION_MISMATCH = "bootstrap_authorization_mismatch"
    BOOTSTRAP_AUTHORIZATION_UNEXPECTED = "bootstrap_authorization_unexpected"
    CONTROL_COMMAND_FAILED = "control_command_failed"


class PreflightRefused(ValueError):
    def __init__(self, reason: PreflightRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TrustedPreflightInput:
    production_authorization_path: Path
    authorization_run_id: WorkflowRunId
    authorization_main_revision: GitCommitSha
    target_revision: GitCommitSha
    target_image_digest: OCIImageDigest
    deploy_directory: Path
    rollback_authorization_path: Path | None
    bootstrap_authorization_path: Path | None
    control_repository: Path


@dataclass(frozen=True, slots=True)
class DockerContainerId:
    value: str

    def __post_init__(self) -> None:
        if _CONTAINER_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid Docker container id")


@dataclass(frozen=True, slots=True)
class DockerImageId:
    value: str

    def __post_init__(self) -> None:
        if _IMAGE_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid Docker image id")


@dataclass(frozen=True, slots=True)
class RunningImageObservation:
    container_id: DockerContainerId
    image_id: DockerImageId
    image_digest: OCIImageDigest
    revision: GitCommitSha


@dataclass(frozen=True, slots=True)
class TrustedPreflightOutcome:
    source_transition: SourceTransition | None
    running_image: RunningImageObservation | None
    payments_floor_proved: bool


def _run(
    command: list[str], *, cwd: Path | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightRefused(PreflightRefusal.CONTROL_COMMAND_FAILED) from None


def _verify_authorization(inputs: TrustedPreflightInput) -> None:
    record = read_production_authorization(inputs.production_authorization_path)
    verify_production_authorization(
        record,
        expected_authorization_run_id=inputs.authorization_run_id,
        expected_authorization_main_revision=inputs.authorization_main_revision,
        expected_release_revision=inputs.target_revision,
        expected_image_digest=inputs.target_image_digest,
    )


def _inspect_running_image() -> RunningImageObservation | None:
    inventory = _run(
        [
            "docker",
            "container",
            "ls",
            "-a",
            "--filter",
            f"name=^/{_APP_CONTAINER}$",
            "--format",
            "{{.Names}}",
        ]
    )
    if inventory.returncode != 0:
        raise PreflightRefused(PreflightRefusal.CONTAINER_INVENTORY_UNREADABLE)
    names = inventory.stdout.splitlines()
    if not names:
        return None
    if names != [_APP_CONTAINER]:
        raise PreflightRefused(PreflightRefusal.CONTAINER_INVENTORY_AMBIGUOUS)

    container_result = _run(
        ["docker", "inspect", "--format", "{{json .}}", _APP_CONTAINER]
    )
    if container_result.returncode != 0:
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    try:
        container_data = json.loads(container_result.stdout)
        if type(container_data) is not dict:
            raise ValueError
        container_id = container_data.get("Id")
        container_name = container_data.get("Name")
        image_id = container_data.get("Image")
        container_config = container_data.get("Config")
        state = container_data.get("State")
        if type(container_config) is not dict or type(state) is not dict:
            raise ValueError
        if state.get("Running") is not True:
            raise ValueError
        config_image = container_config.get("Image")
        container_labels = container_config.get("Labels")
        if type(container_labels) is not dict:
            raise ValueError
        container_revision = container_labels.get(_REVISION_LABEL)
        if (
            type(container_id) is not str
            or _CONTAINER_ID_PATTERN.fullmatch(container_id) is None
            or container_name != f"/{_APP_CONTAINER}"
            or type(image_id) is not str
            or _IMAGE_ID_PATTERN.fullmatch(image_id) is None
            or type(config_image) is not str
            or not config_image.startswith(f"{_IMAGE_REPOSITORY}@sha256:")
            or type(container_revision) is not str
            or _REVISION_PATTERN.fullmatch(container_revision) is None
        ):
            raise ValueError
        image_digest = OCIImageDigest(
            config_image.removeprefix(f"{_IMAGE_REPOSITORY}@")
        )
        typed_container_id = DockerContainerId(container_id)
        typed_image_id = DockerImageId(image_id)
        revision = GitCommitSha(container_revision)
    except (KeyError, TypeError, ValueError):
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED) from None

    image_result = _run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            typed_image_id.value,
        ]
    )
    if image_result.returncode != 0:
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    try:
        image_data = json.loads(image_result.stdout)
        if type(image_data) is not dict:
            raise ValueError
        if image_data.get("Id") != typed_image_id.value:
            raise ValueError
        image_config = image_data.get("Config")
        image_repo_digests = image_data.get("RepoDigests")
        if type(image_config) is not dict or type(image_repo_digests) is not list:
            raise ValueError
        image_labels = image_config.get("Labels")
        if type(image_labels) is not dict:
            raise ValueError
        if image_labels.get(_REVISION_LABEL) != revision.value:
            raise ValueError
        expected_reference = f"{_IMAGE_REPOSITORY}@{image_digest.value}"
        if expected_reference not in image_repo_digests:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED) from None
    return RunningImageObservation(
        typed_container_id, typed_image_id, image_digest, revision
    )


def _classify_transition(
    running_revision: GitCommitSha,
    target_revision: GitCommitSha,
    *,
    repository: Path,
) -> SourceTransition:
    if running_revision == target_revision:
        return SourceTransition.SAME
    target_from_running = _run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            running_revision.value,
            target_revision.value,
        ],
        cwd=repository,
    )
    running_from_target = _run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            target_revision.value,
            running_revision.value,
        ],
        cwd=repository,
    )
    if target_from_running.returncode not in (
        0,
        1,
    ) or running_from_target.returncode not in (
        0,
        1,
    ):
        raise PreflightRefused(PreflightRefusal.SOURCE_RELATION_UNPROVED)
    if target_from_running.returncode == 0:
        return SourceTransition.FORWARD
    if running_from_target.returncode == 0:
        return SourceTransition.BACKWARD
    return SourceTransition.DIVERGENT


def _verify_transition_authorization(
    path: Path,
    *,
    running_revision: GitCommitSha,
    target_revision: GitCommitSha,
) -> None:
    try:
        verify_rollback_authorization(
            read_rollback_authorization(path),
            running_revision=running_revision,
            target_revision=target_revision,
        )
    except Exception:
        raise PreflightRefused(
            PreflightRefusal.TRANSITION_AUTHORIZATION_MISMATCH
        ) from None


def _verify_bootstrap_authorization(path: Path, target_revision: GitCommitSha) -> None:
    try:
        verify_bootstrap_authorization(
            read_bootstrap_authorization(path),
            target_revision=target_revision,
            target_server=ProductionServerName("dotmac-sub-prod"),
        )
    except Exception:
        raise PreflightRefused(
            PreflightRefusal.BOOTSTRAP_AUTHORIZATION_MISMATCH
        ) from None


_FLOOR_PROBE_CODE: Final = (
    "import runpy,sys; sys.path.insert(0,'/app'); "
    "runpy.run_module('scripts.verify_payments_intent_rollback',run_name='__main__')"
)


def _effective_database_url(deploy_directory: Path) -> str:
    env_path = deploy_directory / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED) from None
    values = [
        line.partition("=")[2] for line in lines if line.startswith("DATABASE_URL=")
    ]
    file_value = values[-1] if values else ""
    environment_value = os.environ.get("DATABASE_URL")
    if environment_value is not None and environment_value != file_value:
        raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
    value = environment_value if environment_value is not None else file_value
    if not value or "$" in value or any(character.isspace() for character in value):
        raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
    return value


def _runtime_database_url(observation: RunningImageObservation) -> str:
    container = _inspect_container_by_id(observation.container_id)
    if container != observation:
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    result = _run(
        ["docker", "inspect", "--format", "{{json .}}", observation.container_id.value]
    )
    if result.returncode != 0:
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    try:
        data = json.loads(result.stdout)
        config = data["Config"]
        environment = config["Env"]
        if type(config) is not dict or type(environment) is not list:
            raise ValueError
        entries = [value for value in environment if type(value) is str]
        urls = [
            value.partition("=")[2]
            for value in entries
            if value.startswith("DATABASE_URL=")
        ]
        if len(urls) != 1 or not urls[0] or "\n" in urls[0] or "\r" in urls[0]:
            raise ValueError
        if any(
            value.startswith("LD_PRELOAD=") or value.startswith("LD_LIBRARY_PATH=")
            for value in entries
        ):
            raise ValueError
        return urls[0]
    except (KeyError, TypeError, ValueError):
        raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED) from None


def _inspect_floor_probe(
    probe_name: str, observation: RunningImageObservation, database_url: str
) -> dict[str, object]:
    result = _run(["docker", "inspect", "--format", "{{json .}}", probe_name])
    if result.returncode != 0:
        raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
    try:
        value = json.loads(result.stdout)
        config = value["Config"]
        host = value["HostConfig"]
        mounts = value["Mounts"]
        env = config["Env"]
        labels = config["Labels"]
        if (
            type(config) is not dict
            or type(host) is not dict
            or type(mounts) is not list
            or type(env) is not list
            or type(labels) is not dict
            or value.get("Image") != observation.image_id.value
            or value.get("Name") != f"/{probe_name}"
            or host.get("ReadonlyRootfs") is not True
            or host.get("NetworkMode") != f"container:{observation.container_id.value}"
            or host.get("Privileged") is not False
            or host.get("PidMode") not in (None, "")
            or host.get("IpcMode") not in (None, "", "private")
            or host.get("UTSMode") not in (None, "")
            or host.get("CapAdd") not in (None, [])
            or type(host.get("CapDrop")) is not list
            or "ALL" not in host["CapDrop"]
            or "no-new-privileges:true" not in host.get("SecurityOpt", [])
            or host.get("Binds") not in (None, [])
            or host.get("VolumesFrom") not in (None, [])
            or host.get("Mounts") not in (None, [])
            or host.get("ExtraHosts") not in (None, [])
            or mounts
            or config.get("WorkingDir") != "/app"
            or config.get("Entrypoint") != ["/opt/venv/bin/python"]
            or config.get("Cmd") != ["-I", "-B", "-c", _FLOOR_PROBE_CODE]
            or any(type(item) is not str for item in env)
        ):
            raise ValueError
        entries = [item for item in env if type(item) is str]
        urls = [
            item.partition("=")[2]
            for item in entries
            if item.startswith("DATABASE_URL=")
        ]
        if len(urls) != 1 or urls[0] != database_url:
            raise ValueError
        if any(
            item.startswith("LD_PRELOAD=")
            and item.partition("=")[2]
            or item.startswith("LD_LIBRARY_PATH=")
            and item.partition("=")[2]
            for item in entries
        ):
            raise ValueError
        state = value["State"]
        if type(state) is not dict:
            raise ValueError
        return state
    except (KeyError, TypeError, ValueError):
        raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED) from None


def _verify_payments_floor(
    observation: RunningImageObservation, *, deploy_directory: Path
) -> None:
    if _inspect_container_by_id(observation.container_id) != observation:
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    running_url = _runtime_database_url(observation)
    deployment_url = _effective_database_url(deploy_directory)
    if running_url != deployment_url:
        raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
    probe_name = f"dotmac-sub-floor-probe-{uuid.uuid4().hex}"
    env_file = f"DATABASE_URL={running_url}\n"
    try:
        create = _run(
            [
                "docker",
                "create",
                "--name",
                probe_name,
                "--read-only",
                "--network",
                f"container:{observation.container_id.value}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "/opt/venv/bin/python",
                "--workdir",
                "/app",
                "--env-file",
                "/dev/stdin",
                observation.image_id.value,
                "-I",
                "-B",
                "-c",
                _FLOOR_PROBE_CODE,
            ],
            input_text=env_file,
        )
        if create.returncode != 0:
            raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
        _inspect_floor_probe(probe_name, observation, running_url)
        start = _run(["docker", "start", "--attach", probe_name])
        if start.returncode != 0:
            raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
        state = _inspect_floor_probe(probe_name, observation, running_url)
        if (
            state.get("Running") is not False
            or state.get("ExitCode") != 0
            or state.get("OOMKilled") is not False
        ):
            raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
    finally:
        cleanup = _run(["docker", "rm", "--force", probe_name])
        if cleanup.returncode != 0:
            raise PreflightRefused(PreflightRefusal.PAYMENTS_FLOOR_REFUSED)
    if _inspect_container_by_id(observation.container_id) != observation:
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)


def _inspect_container_by_id(
    container_id: DockerContainerId,
) -> RunningImageObservation:
    result = _run(["docker", "inspect", "--format", "{{json .}}", container_id.value])
    if result.returncode != 0:
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    try:
        data = json.loads(result.stdout)
        if type(data) is not dict:
            raise ValueError
        config = data.get("Config")
        container_name = data.get("Name")
        state = data.get("State")
        if (
            type(config) is not dict
            or type(state) is not dict
            or type(config.get("Labels")) is not dict
        ):
            raise ValueError
        revision = config["Labels"].get(_REVISION_LABEL)
        config_image = config.get("Image")
        image_id = data.get("Image")
        if (
            data.get("Id") != container_id.value
            or container_name != f"/{_APP_CONTAINER}"
            or state.get("Running") is not True
            or type(revision) is not str
            or _REVISION_PATTERN.fullmatch(revision) is None
            or type(config_image) is not str
            or not config_image.startswith(f"{_IMAGE_REPOSITORY}@sha256:")
            or type(image_id) is not str
            or _IMAGE_ID_PATTERN.fullmatch(image_id) is None
        ):
            raise ValueError
        digest = OCIImageDigest(config_image.removeprefix(f"{_IMAGE_REPOSITORY}@"))
        typed_image_id = DockerImageId(image_id)
        typed_revision = GitCommitSha(revision)
    except (KeyError, TypeError, ValueError):
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    return RunningImageObservation(
        container_id,
        typed_image_id,
        digest,
        typed_revision,
    )


def run_preflight(inputs: TrustedPreflightInput) -> TrustedPreflightOutcome:
    try:
        _verify_authorization(inputs)
    except Exception:
        raise PreflightRefused(PreflightRefusal.AUTHORIZATION_UNPROVED) from None
    observation = _inspect_running_image()
    if observation is None:
        if inputs.bootstrap_authorization_path is None:
            raise PreflightRefused(PreflightRefusal.BOOTSTRAP_AUTHORIZATION_REQUIRED)
        _verify_bootstrap_authorization(
            inputs.bootstrap_authorization_path, inputs.target_revision
        )
        if inputs.rollback_authorization_path is not None:
            raise PreflightRefused(PreflightRefusal.BOOTSTRAP_AUTHORIZATION_UNEXPECTED)
        return TrustedPreflightOutcome(None, None, False)
    if inputs.bootstrap_authorization_path is not None:
        raise PreflightRefused(PreflightRefusal.BOOTSTRAP_AUTHORIZATION_UNEXPECTED)
    if (
        observation.image_digest == inputs.target_image_digest
        and observation.revision != inputs.target_revision
    ):
        raise PreflightRefused(PreflightRefusal.IMAGE_IDENTITY_UNPROVED)
    transition = _classify_transition(
        observation.revision,
        inputs.target_revision,
        repository=inputs.control_repository,
    )
    if transition in (SourceTransition.SAME, SourceTransition.FORWARD):
        if inputs.rollback_authorization_path is not None:
            raise PreflightRefused(PreflightRefusal.TRANSITION_AUTHORIZATION_MISMATCH)
        return TrustedPreflightOutcome(transition, observation, False)
    if inputs.rollback_authorization_path is None:
        raise PreflightRefused(PreflightRefusal.TRANSITION_AUTHORIZATION_REQUIRED)
    _verify_transition_authorization(
        inputs.rollback_authorization_path,
        running_revision=observation.revision,
        target_revision=inputs.target_revision,
    )
    _verify_payments_floor(observation, deploy_directory=inputs.deploy_directory)
    return TrustedPreflightOutcome(transition, observation, True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-authorization", type=Path, required=True)
    parser.add_argument("--authorization-run-id", type=int, required=True)
    parser.add_argument("--authorization-main-revision", required=True)
    parser.add_argument("--target-revision", required=True)
    parser.add_argument("--target-image-digest", required=True)
    parser.add_argument("--deploy-directory", type=Path, required=True)
    parser.add_argument("--rollback-authorization", type=Path)
    parser.add_argument("--bootstrap-authorization", type=Path)
    parser.add_argument("--control-repository", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = TrustedPreflightInput(
            production_authorization_path=args.production_authorization,
            authorization_run_id=WorkflowRunId(args.authorization_run_id),
            authorization_main_revision=GitCommitSha(args.authorization_main_revision),
            target_revision=GitCommitSha(args.target_revision),
            target_image_digest=OCIImageDigest(args.target_image_digest),
            deploy_directory=args.deploy_directory,
            rollback_authorization_path=args.rollback_authorization,
            bootstrap_authorization_path=args.bootstrap_authorization,
            control_repository=args.control_repository,
        )
        run_preflight(inputs)
    except PreflightRefused as error:
        reason = error.reason.value
        print(f"TRUSTED PRODUCTION PREFLIGHT REFUSED: {reason}", file=sys.stderr)
        return 2
    except Exception:
        reason = PreflightRefusal.AUTHORIZATION_UNPROVED.value
        print(f"TRUSTED PRODUCTION PREFLIGHT REFUSED: {reason}", file=sys.stderr)
        return 2
    print("Trusted production preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
