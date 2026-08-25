from __future__ import annotations

from pathlib import Path

import pytest

from scripts import new_migration


def _write_revision(
    versions: Path,
    revision: str,
    down_revision: str | tuple[str, ...] | None,
    *,
    depends_on: str | None = None,
) -> None:
    (versions / f"{revision}.py").write_text(
        "\n".join(
            (
                f"revision = {revision!r}",
                f"down_revision = {down_revision!r}",
                "branch_labels = None",
                f"depends_on = {depends_on!r}",
            )
        )
        + "\n"
    )


def _configure_host(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    versions = root / "alembic" / "versions"
    versions.mkdir(parents=True)
    monkeypatch.setattr(new_migration, "REPO_ROOT", root)
    monkeypatch.setattr(new_migration, "VERSIONS", versions)
    return versions


def test_main_allocates_from_host_head_when_modules_are_composed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_versions = _configure_host(monkeypatch, tmp_path)
    module_versions = tmp_path / "module" / "versions"
    module_versions.mkdir(parents=True)

    _write_revision(host_versions, "554_host_provider", None)
    _write_revision(host_versions, "555_dependency_provider", "554_host_provider")
    _write_revision(
        module_versions,
        "pay_0001",
        None,
        depends_on="555_dependency_provider",
    )
    (tmp_path / "alembic.ini").write_text(
        "[alembic]\n"
        f"script_location = {tmp_path / 'alembic'}\n"
        f"version_locations = {host_versions} {module_versions}\n"
    )

    assert new_migration.main(["host_change"]) == 0

    generated = host_versions / "556_host_change.py"
    assert generated.is_file()
    assert 'revision: str = "556_host_change"' in generated.read_text()
    assert 'down_revision: str | None = "555_dependency_provider"' in (
        generated.read_text()
    )


def test_resolve_head_still_refuses_a_real_host_lineage_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_versions = _configure_host(monkeypatch, tmp_path)
    _write_revision(host_versions, "554_host_provider", None)
    _write_revision(host_versions, "555_left", "554_host_provider")
    _write_revision(host_versions, "555_right", "554_host_provider")

    with pytest.raises(SystemExit, match="host lineage is not single-headed"):
        new_migration._resolve_head(new_migration._script_directory())
