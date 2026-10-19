"""Read-only baseline observations fail closed on ambiguous running identity."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.pr3659_release_preflight import (
    BaselineObservationError,
    DockerReader,
    ObserveProductionBaselineCommand,
    observe_production_baseline,
    write_baseline,
)
from scripts.release_artifact_contract import (
    GitCommitSha,
    ProductionServerName,
    WorkflowRunId,
)


def _command(tmp_path: Path) -> ObserveProductionBaselineCommand:
    deploy = tmp_path / "deployment"
    deploy.mkdir()
    (deploy / ".env").write_text(
        "APP_ENV=production\nSERVER_NAME=dotmac-sub-prod\nSECRET_KEY=not-for-output\n"
    )
    return ObserveProductionBaselineCommand(
        ProductionServerName("dotmac-sub-prod"),
        GitCommitSha("a" * 40),
        WorkflowRunId(123),
        deploy,
        tmp_path / "evidence/baseline.json",
        datetime(2026, 10, 19, tzinfo=UTC),
    )


def _reader(
    *, changed: bool = False, digest_image: str | None = None, running: bool = True
) -> tuple[DockerReader, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []

    def read(argv: tuple[str, ...]) -> str:
        calls.append(argv)
        if argv[:3] == ("docker", "container", "inspect"):
            return json.dumps(
                {
                    "container_id": ("b" if changed and len(calls) == 4 else "c") * 64,
                    "image_id": "sha256:" + "d" * 64,
                    "running": running,
                }
            )
        if argv[-1] == "{{.Id}}":
            return digest_image or "sha256:" + "d" * 64
        assert argv[:3] == ("docker", "image", "inspect")
        return json.dumps(
            {
                "id": "sha256:" + "d" * 64,
                "revision": "1" * 40,
                "tree": "2" * 40,
                "repo_digests": ["ghcr.io/michaelayoade/dotmac_sub@sha256:" + "3" * 64],
            }
        )

    return read, calls


def test_observer_binds_running_image_to_local_registry_digest_without_mutation(
    tmp_path: Path,
) -> None:
    command = _command(tmp_path)
    read, calls = _reader()
    result = observe_production_baseline(command=command, read=read)
    write_baseline(baseline=result, path=command.output_path)
    content = command.output_path.read_text()
    assert result.source_revision.value == "1" * 40
    assert len(calls) == 4 and all(argv[2] == "inspect" for argv in calls)
    assert "not-for-output" not in content and "SECRET_KEY" not in content
    assert json.loads(content)["kind"] == "pr3659-production-baseline-observation"


@pytest.mark.parametrize("changed,running", [(True, True), (False, False)])
def test_changed_or_stopped_container_is_not_accepted(
    tmp_path: Path, changed: bool, running: bool
) -> None:
    read, _calls = _reader(changed=changed, running=running)
    with pytest.raises(BaselineObservationError):
        observe_production_baseline(command=_command(tmp_path), read=read)


def test_different_digest_resolution_is_not_accepted(tmp_path: Path) -> None:
    read, _calls = _reader(digest_image="sha256:" + "e" * 64)
    with pytest.raises(BaselineObservationError, match="running image"):
        observe_production_baseline(command=_command(tmp_path), read=read)


def test_evidence_cannot_be_written_into_production_deployment_directory(
    tmp_path: Path,
) -> None:
    command = _command(tmp_path)
    read, calls = _reader()
    with pytest.raises(BaselineObservationError, match="outside"):
        observe_production_baseline(
            command=replace(
                command, output_path=command.deploy_directory / "baseline.json"
            ),
            read=read,
        )
    assert not calls
