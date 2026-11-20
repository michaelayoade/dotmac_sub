"""Assign every architecture test module to one bounded CI shard."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE_ROOT = ROOT / "tests" / "architecture"


def architecture_files() -> tuple[Path, ...]:
    return tuple(sorted(ARCHITECTURE_ROOT.rglob("test_*.py")))


def partition_architecture_files(*, shards: int) -> tuple[tuple[Path, ...], ...]:
    if shards < 1:
        raise ValueError("shards must be positive")
    groups: list[list[Path]] = [[] for _ in range(shards)]
    weights = [0] * shards
    for path in sorted(
        architecture_files(), key=lambda item: (-item.stat().st_size, item.as_posix())
    ):
        index = min(range(shards), key=lambda item: (weights[item], item))
        groups[index].append(path)
        weights[index] += path.stat().st_size
    return tuple(tuple(sorted(group)) for group in groups)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    args = parser.parse_args()
    if args.shard < 1 or args.shard > args.shards:
        parser.error("shard must be between 1 and shards")
    files = partition_architecture_files(shards=args.shards)[args.shard - 1]
    if not files:
        parser.error("architecture shard has no test files")
    print(" ".join(path.relative_to(ROOT).as_posix() for path in files))


if __name__ == "__main__":
    main()
