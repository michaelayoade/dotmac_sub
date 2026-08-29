"""Select one deterministic, duration-balanced integration-test shard.

The previous selector balanced by SOURCE FILE SIZE. It achieved near-perfect
byte balance -- 110/110/111/110 KB -- against a 22/6/5/4-minute runtime spread,
because what an integration test costs is dominated by how much of the Alembic
chain it replays, and that has no relationship to how long its source file is.

So this balances by MEASURED duration, the way the unit lane already does. Three
properties matter more than the arithmetic:

- **Scheduling never decides execution.** Missing, corrupt, truncated or hostile
  duration data changes only which shard a file lands in. Every file always
  lands in exactly one shard, so the suite that runs is identical either way.
- **An unmeasured file is assumed expensive.** A new integration test that
  creates its own database and replays the chain costs ~50 s. Guessing "cheap"
  puts it on an already-full shard; guessing "expensive" costs one run of mild
  imbalance and is corrected by the next measurement. Byte size is deliberately
  not used even as a fallback -- it is the heuristic being removed, and a wrong
  number that looks informed is worse than an honest conservative constant.
- **The partition is auditable.** The chosen files and their estimated total go
  to stderr on every run, so a reviewer reading CI logs can see what a shard was
  asked to do without re-deriving it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_ROOT = REPOSITORY_ROOT / "tests" / "integration"
DEFAULT_DURATIONS_PATH = REPOSITORY_ROOT / ".ci-cache/integration-test-durations.json"

#: Floor for a file with no measured history, in seconds. Sized to the cost of
#: one full Alembic chain replay, because that is what a new integration test
#: most often does.
UNMEASURED_FILE_SECONDS = 60.0


def integration_files() -> list[Path]:
    """Every integration test module, found RECURSIVELY.

    The previous implementation used a non-recursive `glob`, so the first
    subdirectory anyone added under `tests/integration/` would have been dropped
    from every shard silently -- no error, no skip, just tests that stopped
    running while CI stayed green.
    """

    return sorted(INTEGRATION_ROOT.rglob("test_*.py"))


def load_durations(path: Path | None) -> dict[str, float]:
    """Read measured durations, tolerating anything the file might contain.

    Every rejection path returns data rather than raising. A corrupt cache must
    degrade scheduling, never fail the job -- the tests it would have selected
    still run, just in a less even arrangement.
    """

    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    if payload.get("schema_version") not in (None, 1):
        return {}
    raw = payload.get("durations", payload)
    if not isinstance(raw, Mapping):
        return {}
    durations: dict[str, float] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            continue
        # `bool` is an `int`; a JSON `true` is not a duration.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        # Rejects NaN and both infinities. NaN would poison every comparison in
        # the sort; +inf would monopolise a shard and starve the others.
        if not math.isfinite(number) or number < 0:
            continue
        durations[name] = number
    return durations


def unmeasured_estimate(durations: Mapping[str, float]) -> float:
    """Conservative cost for a file nobody has measured yet.

    The most expensive thing we have actually observed, never below the floor.
    Deliberately pessimistic: under-estimating a new file overloads a real
    shard, while over-estimating spreads the load slightly unevenly for exactly
    one run before measurement replaces the guess.
    """

    if not durations:
        return UNMEASURED_FILE_SECONDS
    return max(UNMEASURED_FILE_SECONDS, max(durations.values()))


def estimate_file_seconds(
    paths: Sequence[Path], durations: Mapping[str, float]
) -> dict[Path, float]:
    fallback = unmeasured_estimate(durations)
    return {
        path: durations.get(path.relative_to(REPOSITORY_ROOT).as_posix(), fallback)
        for path in paths
    }


def partition_integration_files(
    *, shards: int, durations_path: Path | None = DEFAULT_DURATIONS_PATH
) -> tuple[tuple[Path, ...], ...]:
    """Split every integration file across `shards`, each file exactly once.

    Longest-processing-time greedy: heaviest file first, onto whichever shard is
    currently lightest. Both orderings are total -- files break ties on path,
    shards break ties on index -- so the same inputs always produce the same
    partition on every runner.
    """

    if shards < 1:
        raise ValueError("shards must be positive")

    paths = integration_files()
    estimates = estimate_file_seconds(paths, load_durations(durations_path))
    groups: list[list[Path]] = [[] for _ in range(shards)]
    totals = [0.0] * shards

    ordered = sorted(paths, key=lambda path: (-estimates[path], path.as_posix()))
    for path in ordered:
        target = min(range(shards), key=lambda index: (totals[index], index))
        groups[target].append(path)
        totals[target] += estimates[path]

    return tuple(tuple(sorted(group)) for group in groups)


def select_integration_shard(
    *, shard: int, shards: int, durations_path: Path | None = DEFAULT_DURATIONS_PATH
) -> tuple[Path, ...]:
    if shard < 1 or shard > shards:
        raise ValueError(f"shard must be between 1 and {shards}")
    return partition_integration_files(shards=shards, durations_path=durations_path)[
        shard - 1
    ]


def render_audit(*, shard: int, shards: int, durations_path: Path | None) -> str:
    """Human-readable account of what this shard was asked to run and why."""

    durations = load_durations(durations_path)
    paths = integration_files()
    estimates = estimate_file_seconds(paths, durations)
    groups = partition_integration_files(shards=shards, durations_path=durations_path)
    selected = groups[shard - 1]

    measured = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths} & set(
        durations
    )
    lines = [
        f"integration shard {shard}/{shards}: "
        f"{len(selected)} of {len(paths)} files, "
        f"estimated {sum(estimates[path] for path in selected):.0f}s",
        f"duration history: {'absent or unusable' if not durations else str(len(durations)) + ' entries'}"
        f", {len(measured)}/{len(paths)} files measured"
        f", unmeasured assumed {unmeasured_estimate(durations):.0f}s",
    ]
    for index, group in enumerate(groups, start=1):
        total = sum(estimates[path] for path in group)
        marker = "*" if index == shard else " "
        lines.append(f" {marker} shard {index}: {len(group):>3} files, {total:8.0f}s")
    lines.append(f"selected files for shard {shard}:")
    for path in selected:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        source = "measured" if relative in durations else "estimated"
        lines.append(f"    {estimates[path]:8.1f}s  {source:<9} {relative}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument(
        "--durations-file",
        type=Path,
        default=DEFAULT_DURATIONS_PATH,
        help="measured per-file durations; absent or corrupt is tolerated",
    )
    args = parser.parse_args(argv)

    paths = select_integration_shard(
        shard=args.shard, shards=args.shards, durations_path=args.durations_file
    )
    # The audit goes to stderr because stdout is consumed as a pytest argument
    # list by the Makefile; anything printed there becomes a path.
    print(
        render_audit(
            shard=args.shard, shards=args.shards, durations_path=args.durations_file
        ),
        file=sys.stderr,
    )
    print(" ".join(path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths))


if __name__ == "__main__":
    main()
