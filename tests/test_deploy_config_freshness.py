"""The deploy's "did this container's mounted config change?" decision."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts.deploy_config_freshness import (
    InvalidStartTime,
    SourceState,
    StaleSource,
    main,
    parse_container_start,
    stale_sources,
)


def _settle() -> None:
    """Keep the filesystem's coarse timestamp clock on one side of a mark."""

    time.sleep(0.05)


def _mark() -> int:
    _settle()
    mark = time.time_ns()
    _settle()
    return mark


def test_docker_start_time_keeps_nanoseconds() -> None:
    assert parse_container_start("2026-10-05T12:00:00.000000001Z") == (
        parse_container_start("2026-10-05T12:00:00Z") + 1
    )
    assert parse_container_start("2026-10-05T13:00:00.5+01:00") == (
        parse_container_start("2026-10-05T12:00:00Z") + 500_000_000
    )


@pytest.mark.parametrize(
    "value", ["", "yesterday", "2026-10-05 12:00:00", "0001-01-01T00:00:00Z"]
)
def test_unusable_start_time_is_refused(value: str) -> None:
    with pytest.raises(InvalidStartTime):
        parse_container_start(value)


def test_file_replaced_after_start_is_changed(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text("old\n")
    started = _mark()
    # What `git pull` does: write a new inode and rename it into place.
    replacement = tmp_path / ".config.yml.tmp"
    replacement.write_text("new\n")
    replacement.replace(config)

    assert stale_sources(
        sources=[str(config)], within=tmp_path, started_ns=started
    ) == [StaleSource(SourceState.CHANGED, config)]


def test_file_untouched_since_start_is_current(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text("same\n")
    started = _mark()

    assert (
        stale_sources(sources=[str(config)], within=tmp_path, started_ns=started) == []
    )


def test_directory_with_a_changed_nested_file_is_changed(tmp_path: Path) -> None:
    directory = tmp_path / "freeradius"
    (directory / "mods-enabled").mkdir(parents=True)
    nested = directory / "mods-enabled" / "sql"
    nested.write_text("old\n")
    started = _mark()
    nested.write_text("new\n")

    assert stale_sources(
        sources=[str(directory)], within=tmp_path, started_ns=started
    ) == [StaleSource(SourceState.CHANGED, directory)]


def test_directory_with_a_deleted_file_is_changed(tmp_path: Path) -> None:
    directory = tmp_path / "vmagent"
    directory.mkdir()
    (directory / "old.yml").write_text("x\n")
    started = _mark()
    (directory / "old.yml").unlink()

    assert stale_sources(
        sources=[str(directory)], within=tmp_path, started_ns=started
    ) == [StaleSource(SourceState.CHANGED, directory)]


def test_sources_outside_the_deployment_directory_are_ignored(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "deploy"
    checkout.mkdir()
    started = _mark()
    host = tmp_path / "var-lib-docker-containers"
    host.mkdir()
    (host / "log").write_text("appended\n")

    assert (
        stale_sources(
            sources=[str(host), "/var/run/docker.sock", ""],
            within=checkout,
            started_ns=started,
        )
        == []
    )


def test_missing_checkout_source_is_reported(tmp_path: Path) -> None:
    missing = tmp_path / "config" / "gone.conf"

    assert stale_sources(sources=[str(missing)], within=tmp_path, started_ns=0) == [
        StaleSource(SourceState.MISSING, missing)
    ]


def test_cli_prints_one_line_per_stale_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "radiusd.conf"
    stale.write_text("x\n")

    assert (
        main(
            [
                "--started-at",
                "2000-01-01T00:00:00Z",
                "--within",
                str(tmp_path),
                str(stale),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == f"changed {stale}\n"


def test_cli_fails_closed_on_an_unusable_start_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--started-at", "", "--within", str(tmp_path)]) == 2
    assert "CONFIG FRESHNESS" in capsys.readouterr().err
