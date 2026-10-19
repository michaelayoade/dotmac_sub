"""Read-only production-baseline evidence for the requested PR-3659-only release.

This report is not a candidate, staging acceptance, or production authorization.
It deliberately has no mutating Docker command, SSH option, or deploy mode.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.release_artifact_contract import (
    GitCommitSha,
    GitTreeSha,
    OCIImageDigest,
    ProductionServerName,
    WorkflowRunId,
)

OWNER = "release.pr3659_preflight"
IMAGE_REPOSITORY = "ghcr.io/michaelayoade/dotmac_sub"
APP_CONTAINER = "dotmac_sub_app"
DockerReader = Callable[[tuple[str, ...]], str]


class BaselineObservationError(ValueError):
    """The exact running application identity cannot be proven."""


@dataclass(frozen=True, slots=True)
class ObserveProductionBaselineCommand:
    target: ProductionServerName
    authority_revision: GitCommitSha
    observation_run_id: WorkflowRunId
    deploy_directory: Path
    output_path: Path
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ProductionBaseline:
    target: ProductionServerName
    authority_revision: GitCommitSha
    observation_run_id: WorkflowRunId
    observed_at: datetime
    source_revision: GitCommitSha
    source_tree: GitTreeSha
    image_digest: OCIImageDigest
    image_id: str
    container_id: str


def _read(argv: tuple[str, ...]) -> str:
    completed = subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=30
    )
    return completed.stdout.strip()


def _object(value: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BaselineObservationError(
            "Docker returned invalid observation JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise BaselineObservationError("Docker observation is not an object")
    return parsed


def observe_production_baseline(
    *, command: ObserveProductionBaselineCommand, read: DockerReader = _read
) -> ProductionBaseline:
    """Only explicitly selected non-secret Docker fields leave the host."""
    if command.observed_at.tzinfo is None or command.observed_at.utcoffset() is None:
        raise BaselineObservationError("An aware observation time is required")
    deploy = command.deploy_directory.resolve()
    output = command.output_path.resolve()
    if output == deploy or output.is_relative_to(deploy):
        raise BaselineObservationError(
            "Observation output must be outside the deployment directory"
        )
    lines = (deploy / ".env").read_text(encoding="utf-8").splitlines()
    if (
        "APP_ENV=production" not in lines
        or f"SERVER_NAME={command.target.value}" not in lines
    ):
        raise BaselineObservationError("Production host contract does not match")
    row = _object(
        read(
            (
                "docker",
                "container",
                "inspect",
                APP_CONTAINER,
                "--format",
                '{"container_id":{{json .Id}},"image_id":{{json .Image}},"running":{{json .State.Running}}}',
            )
        )
    )
    container_id, image_id = (
        str(row.get("container_id") or ""),
        str(row.get("image_id") or ""),
    )
    if (
        row.get("running") is not True
        or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
    ):
        raise BaselineObservationError(
            "The application is not a uniquely identified running container"
        )
    image = _object(
        read(
            (
                "docker",
                "image",
                "inspect",
                image_id,
                "--format",
                '{"id":{{json .Id}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}},"tree":{{json (index .Config.Labels "io.dotmac.release.source-tree")}},"repo_digests":{{json .RepoDigests}}}',
            )
        )
    )
    if image.get("id") != image_id:
        raise BaselineObservationError("Image identity changed during observation")
    references = image.get("repo_digests")
    if not isinstance(references, list) or not all(
        isinstance(item, str) for item in references
    ):
        raise BaselineObservationError(
            "Running image has no readable registry-digest evidence"
        )
    digests = {
        item.removeprefix(IMAGE_REPOSITORY + "@")
        for item in references
        if item.startswith(IMAGE_REPOSITORY + "@")
    }
    if len(digests) != 1:
        raise BaselineObservationError(
            "Running image must have exactly one digest in the application repository"
        )
    digest = OCIImageDigest(next(iter(digests)))
    resolved = read(
        (
            "docker",
            "image",
            "inspect",
            f"{IMAGE_REPOSITORY}@{digest.value}",
            "--format",
            "{{.Id}}",
        )
    )
    if resolved != image_id:
        raise BaselineObservationError(
            "Registry digest does not resolve locally to the running image"
        )
    # Re-observe the container after reading image evidence to detect a cutover.
    final = _object(
        read(
            (
                "docker",
                "container",
                "inspect",
                APP_CONTAINER,
                "--format",
                '{"container_id":{{json .Id}},"image_id":{{json .Image}},"running":{{json .State.Running}}}',
            )
        )
    )
    if final != row:
        raise BaselineObservationError("Running application changed during observation")
    return ProductionBaseline(
        command.target,
        command.authority_revision,
        command.observation_run_id,
        command.observed_at,
        GitCommitSha(str(image.get("revision") or "")),
        GitTreeSha(str(image.get("tree") or "")),
        digest,
        image_id,
        container_id,
    )


def write_baseline(*, baseline: ProductionBaseline, path: Path) -> None:
    document: dict[str, str | int] = {
        "schema_version": 1,
        "kind": "pr3659-production-baseline-observation",
        "target": baseline.target.value,
        "authority_revision": baseline.authority_revision.value,
        "observation_run_id": baseline.observation_run_id.value,
        "observed_at": baseline.observed_at.isoformat(),
        "source_revision": baseline.source_revision.value,
        "source_tree": baseline.source_tree.value,
        "image_digest": baseline.image_digest.value,
        "image_id": baseline.image_id,
        "container_id": baseline.container_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority-revision", required=True)
    parser.add_argument("--observation-run-id", required=True, type=int)
    parser.add_argument("--deploy-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if _read(("git", "rev-parse", "HEAD")) != args.authority_revision:
        raise BaselineObservationError(
            "Checkout is not the exact protected authority revision"
        )
    command = ObserveProductionBaselineCommand(
        ProductionServerName("dotmac-sub-prod"),
        GitCommitSha(args.authority_revision),
        WorkflowRunId(args.observation_run_id),
        args.deploy_directory,
        args.output,
        datetime.now(UTC),
    )
    baseline = observe_production_baseline(command=command)
    write_baseline(baseline=baseline, path=command.output_path)
    print("Read-only production baseline recorded; no deployment authority was issued.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
