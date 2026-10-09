"""Decide whether a running container started before its checkout config changed.

``scripts/deploy.sh`` never recreates freeradius, vmagent or promtail, and none
of them re-reads its configuration while running. A pulled config change
therefore reaches the process only through a container restart. On
dotmac_erp, vmagent ran a two-week-old config after a ``git pull`` because
nothing restarted it (dotmac_erp PR #695), and production and staging metrics
were merged under one label.

The deploy asks one question per service: *did any checkout file this
container bind-mounts change after the container started?* If so, the running
process may be using configuration that no longer matches the checkout, and a
restart (which re-resolves every bind-mount source path) is needed.

Why timestamps and not ``git diff <previous release> <new release>``: the
files a container mounts come from the deployment directory, and on production
that directory is a host-owned checkout that the deploy does not move (the
release Compose file comes from the Actions workspace, ``config/`` from
``DEPLOY_DIR``; see docs/runbooks/PRODUCTION_DEPLOYMENT.md). A diff between
two release revisions would restart a service whose mounted files never
changed, and miss a host checkout that was pulled between deploys. Comparing
the mounted files' inode change time (``st_ctime``) with the container's start
time asks the real question on both hosts and needs no recorded state: Git
writes a changed file as a new inode, which always carries a fresh ctime, and
leaves unchanged files untouched. A false positive (a file rewritten with
identical content, a chmod) costs one unnecessary restart; it can never hide a
change.

Only sources inside ``--within`` (the deployment directory) are considered, so
host paths such as ``/var/run/docker.sock`` or ``/var/lib/docker/containers``
(which promtail mounts and which change constantly) are ignored.

Usage::

    python -m scripts.deploy_config_freshness \\
        --started-at 2026-10-05T12:34:56.123456789Z \\
        --within /root/dotmac_sub -- <bind source> [<bind source> ...]

Prints one ``changed <path>`` or ``missing <path>`` line per stale source and
exits 0. Exits 2 on an unusable start time, so the caller can fail closed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

_RFC3339 = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})$"
)
#: Docker reports this start time for a container that never started.
_NEVER_STARTED_YEAR = 1


class InvalidStartTime(ValueError):
    """The container start time is not a usable RFC 3339 instant."""


class SourceState(str, Enum):
    """Why a mounted source makes the running container stale."""

    CHANGED = "changed"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class StaleSource:
    """One bind-mount source whose content may differ from what was loaded."""

    state: SourceState
    path: Path

    def render(self) -> str:
        return f"{self.state.value} {self.path}"


def parse_container_start(value: str) -> int:
    """Return a Docker ``State.StartedAt`` value as integer nanoseconds.

    Docker prints nanosecond precision, which ``datetime`` cannot hold, so the
    fraction is carried separately instead of being rounded away: rounding
    down could report a file written in the same second as already loaded.
    """

    match = _RFC3339.match(value.strip())
    if match is None:
        raise InvalidStartTime(f"unrecognised container start time {value!r}")
    zone = match["zone"]
    # timezone.utc, not datetime.UTC: this runs on the deploy host's python3,
    # which may predate 3.11.
    base = datetime.fromisoformat(
        match["base"] + ("+00:00" if zone == "Z" else zone)
    ).astimezone(timezone.utc)  # noqa: UP017
    if base.year == _NEVER_STARTED_YEAR:
        raise InvalidStartTime("container has never started")
    fraction = (match["fraction"] or "").ljust(9, "0")
    return int(base.timestamp()) * 1_000_000_000 + int(fraction or "0")


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _walk(source: Path) -> Iterator[Path]:
    """Yield the source and, for a directory, everything beneath it.

    Directories are included because adding or removing a file changes the
    directory's own ctime, which is the only trace a deletion leaves.
    """

    yield source
    if source.is_dir() and not source.is_symlink():
        for directory, subdirectories, files in os.walk(source):
            base = Path(directory)
            for name in (*subdirectories, *files):
                yield base / name


def _changed_after(path: Path, started_ns: int) -> bool:
    try:
        return os.lstat(path).st_ctime_ns > started_ns
    except FileNotFoundError:
        # Removed while walking: the tree changed after the container started.
        return True


def stale_sources(
    *, sources: Sequence[str], within: Path, started_ns: int
) -> list[StaleSource]:
    """Return the in-checkout sources that changed after ``started_ns``."""

    root = within.resolve()
    stale: list[StaleSource] = []
    seen: set[Path] = set()
    for raw in sources:
        if not raw:
            continue
        source = Path(raw)
        resolved = source.resolve()
        if not _is_within(resolved, root) or resolved in seen:
            continue
        seen.add(resolved)
        if not source.exists():
            stale.append(StaleSource(SourceState.MISSING, source))
            continue
        if any(_changed_after(path, started_ns) for path in _walk(source)):
            stale.append(StaleSource(SourceState.CHANGED, source))
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--within", required=True, type=Path)
    parser.add_argument("sources", nargs="*")
    arguments = parser.parse_args(argv)
    try:
        started_ns = parse_container_start(arguments.started_at)
    except InvalidStartTime as error:
        print(f"CONFIG FRESHNESS: {error}", file=sys.stderr)
        return 2
    for item in stale_sources(
        sources=arguments.sources, within=arguments.within, started_ns=started_ns
    ):
        print(item.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
