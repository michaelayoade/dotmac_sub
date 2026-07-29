"""Select a deterministic, approximately balanced CI unit-test shard."""

from __future__ import annotations

import argparse
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = REPOSITORY_ROOT / "tests"
EXCLUDED_PARTS = {"architecture", "e2e", "integration", "playwright"}


def _test_files() -> list[Path]:
    return [
        path
        for path in TEST_ROOT.rglob("test_*.py")
        if not EXCLUDED_PARTS.intersection(path.relative_to(TEST_ROOT).parts)
    ]


def select_shard(*, shard: int, shards: int) -> list[Path]:
    """Greedily balance files by size while keeping assignment deterministic."""
    if shards < 1:
        raise ValueError("shards must be positive")
    if shard < 1 or shard > shards:
        raise ValueError(f"shard must be between 1 and {shards}")

    groups: list[list[Path]] = [[] for _ in range(shards)]
    weights = [0] * shards
    weighted_files = sorted(
        ((path.stat().st_size, path) for path in _test_files()),
        key=lambda item: (-item[0], item[1].as_posix()),
    )
    for weight, path in weighted_files:
        target = min(range(shards), key=lambda index: (weights[index], index))
        groups[target].append(path)
        weights[target] += weight
    return sorted(groups[shard - 1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    args = parser.parse_args()
    paths = select_shard(shard=args.shard, shards=args.shards)
    print(" ".join(path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths))


if __name__ == "__main__":
    main()
