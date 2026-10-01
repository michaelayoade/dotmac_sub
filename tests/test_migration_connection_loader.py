"""Host-local migration URL handoff without a database or network connection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import with_migration_connection as loader

REPO = Path(__file__).resolve().parents[1]
URL = "postgresql+psycopg://app_admin@postgres-local:5432/dotmac_sub"


def _held_file(tmp_path: Path, value: str = URL) -> Path:
    path = tmp_path / "migration-url"
    path.write_text(value, encoding="utf-8")
    path.chmod(0o400)
    return path


def _environment(path: Path) -> dict[str, str]:
    return {"MIGRATION_DATABASE_URL_FILE": str(path)}


def test_valid_file_reaches_only_child_environment_not_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _held_file(tmp_path)
    for name in (
        "MIGRATION_DATABASE_URL",
        "MIGRATION_DATABASE_URL_FILE",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL_FILE", str(path))
    captured: dict[str, object] = {}

    class ReachedExec(Exception):
        pass

    def fake_exec(executable: str, argv: list[str], child_env: dict[str, str]) -> None:
        captured.update(executable=executable, argv=argv, child_env=child_env)
        raise ReachedExec

    monkeypatch.setattr(loader.os, "execvpe", fake_exec)
    with pytest.raises(ReachedExec):
        loader.main(["--", "bash", "scripts/deploy_staging.sh", "sha256:candidate"])
    assert captured["executable"] == "bash"
    assert captured["argv"] == ["bash", "scripts/deploy_staging.sh", "sha256:candidate"]
    assert URL not in repr(captured["argv"])
    child_env = captured["child_env"]
    assert isinstance(child_env, dict)
    assert child_env["MIGRATION_DATABASE_URL"] == URL
    assert "MIGRATION_DATABASE_URL_FILE" not in child_env
    assert "DATABASE_URL" not in child_env
    assert "MIGRATION_DATABASE_URL" not in os.environ
    assert URL not in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [
        "",
        URL + "\n",
        " " + URL,
        "mysql://app_admin@postgres-local/dotmac_sub",
        "postgresql://app_user@postgres-local/dotmac_sub",
        "postgresql://app_admin@postgres-local/",
        "postgresql://app_admin@postgres-local/%ZZ",
        "postgresql://app_admin@postgres-local/dotmac_sub#fragment",
    ],
)
def test_malformed_content_fails_without_echo(tmp_path: Path, content: str) -> None:
    path = _held_file(tmp_path, content)
    with pytest.raises(loader.LoaderRefusal) as error:
        loader.load_migration_url(_environment(path))
    assert error.value.code in {"invalid_file_authority", "invalid_url"}
    if content:
        assert content not in str(error.value)


@pytest.mark.parametrize(
    "pointer", ["", "relative/file", " " + os.sep + "tmp/file", os.sep + "tmp/a\nb"]
)
def test_malformed_pointer_fails(pointer: str) -> None:
    with pytest.raises(loader.LoaderRefusal, match="invalid_pointer"):
        loader.load_migration_url({"MIGRATION_DATABASE_URL_FILE": pointer})


def test_missing_unreadable_symlink_mode_owner_and_size_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing"
    with pytest.raises(loader.LoaderRefusal, match="unreadable_file"):
        loader.load_migration_url(_environment(path))
    path = _held_file(tmp_path)
    real_open = loader.os.open

    def denied_open(candidate: os.PathLike[str], flags: int) -> int:
        if candidate == path:
            raise PermissionError("test-only denied read")
        return real_open(candidate, flags)

    monkeypatch.setattr(loader.os, "open", denied_open)
    with pytest.raises(loader.LoaderRefusal, match="unreadable_file"):
        loader.load_migration_url(_environment(path))
    monkeypatch.undo()
    link = tmp_path / "symlink"
    link.symlink_to(path)
    with pytest.raises(loader.LoaderRefusal, match="unreadable_file"):
        loader.load_migration_url(_environment(link))
    path.chmod(0o600)
    with pytest.raises(loader.LoaderRefusal, match="invalid_file_authority"):
        loader.load_migration_url(_environment(path))
    path.chmod(0o400)
    current_uid = os.geteuid()
    monkeypatch.setattr(loader.os, "geteuid", lambda: current_uid + 1)
    with pytest.raises(loader.LoaderRefusal, match="invalid_file_authority"):
        loader.load_migration_url(_environment(path))
    monkeypatch.undo()
    path.chmod(0o600)
    path.write_bytes(b"x" * (loader.MAX_URL_BYTES + 1))
    path.chmod(0o400)
    with pytest.raises(loader.LoaderRefusal, match="invalid_file_authority"):
        loader.load_migration_url(_environment(path))


def test_fifo_and_device_are_refused_without_waiting_for_fifo_writer(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "named-pipe"
    os.mkfifo(fifo, mode=0o400)
    with pytest.raises(loader.LoaderRefusal, match="invalid_file_authority"):
        loader.load_migration_url(_environment(fifo))
    with pytest.raises(loader.LoaderRefusal, match="invalid_file_authority"):
        loader.load_migration_url(_environment(Path(os.devnull)))


def test_existing_url_and_deploy_path_fail(
    tmp_path: Path,
) -> None:
    path = _held_file(tmp_path)
    environment = _environment(path)
    with pytest.raises(loader.LoaderRefusal, match="existing_url_conflict"):
        loader.load_migration_url({**environment, "MIGRATION_DATABASE_URL": ""})
    with pytest.raises(loader.LoaderRefusal, match="source_or_deploy_path"):
        loader.load_migration_url({**environment, "STAGING_DEPLOY_DIR": str(tmp_path)})
    with pytest.raises(loader.LoaderRefusal, match="source_or_deploy_path"):
        loader.load_migration_url(
            {"MIGRATION_DATABASE_URL_FILE": str(REPO / "pyproject.toml")}
        )


@pytest.mark.parametrize("inherited", ["", "postgresql://app_user@db/dotmac_sub", URL])
def test_inherited_runtime_url_refused_before_exec_even_when_empty(
    tmp_path: Path,
    inherited: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _held_file(tmp_path)
    for name in (
        "MIGRATION_DATABASE_URL",
        "MIGRATION_DATABASE_URL_FILE",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL_FILE", str(path))
    monkeypatch.setenv("DATABASE_URL", inherited)

    def unexpected_exec(*_args: object) -> None:
        raise AssertionError("loader reached child exec with inherited runtime URL")

    monkeypatch.setattr(loader.os, "execvpe", unexpected_exec)
    assert loader.main(["--", "bash", "scripts/deploy_staging.sh", "candidate"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "migration connection loader refused: inherited_runtime_url\n"
    if inherited:
        assert inherited not in output.err


def test_workflows_use_loader_and_runtime_compose_masks_url() -> None:
    staging = (REPO / ".github/workflows/staging-deploy.yml").read_text()
    production = (REPO / ".github/workflows/production-deploy.yml").read_text()
    assert (
        "MIGRATION_DATABASE_URL_FILE: ${{ vars.MIGRATION_DATABASE_URL_FILE }}"
        in staging
    )
    assert (
        "MIGRATION_DATABASE_URL_FILE: ${{ vars.MIGRATION_DATABASE_URL_FILE }}"
        in production
    )
    assert "python scripts/with_migration_connection.py --" in staging
    assert 'bash scripts/deploy_staging.sh "$IMAGE_DIGEST"' in staging
    assert "python scripts/with_migration_connection.py --" in production
    assert 'bash scripts/deploy_production.sh "${args[@]}"' in production
    compose = (REPO / "docker-compose.yml").read_text()
    migration_lines = [
        line.strip()
        for line in compose.splitlines()
        if line.strip().startswith("MIGRATION_DATABASE_URL:")
    ]
    assert migration_lines and set(migration_lines) == {"MIGRATION_DATABASE_URL: ''"}
