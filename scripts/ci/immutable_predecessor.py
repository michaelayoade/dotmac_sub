"""Acquire and verify the exact old source used by the PostgreSQL upgrade canary.

Git ancestry is not source identity: a squash merge need not retain the original
feature base in the candidate's ancestry. No branch or tag substitutes for the
reviewed commit and tree, and local fixture verification never fetches.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PredecessorRefusal(StrEnum):
    INVALID_PIN = "invalid_predecessor_pin"
    MISSING_OBJECT = "predecessor_object_missing"
    NOT_COMMIT = "predecessor_object_not_commit"
    WRONG_TREE = "predecessor_tree_mismatch"
    FETCH_FAILED = "predecessor_acquisition_failed"


class PredecessorError(RuntimeError):
    def __init__(self, code: PredecessorRefusal) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class ImmutablePredecessor:
    commit: str
    tree: str

    def __post_init__(self) -> None:
        if not all(
            re.fullmatch(r"[0-9a-f]{40}", value) for value in (self.commit, self.tree)
        ):
            raise PredecessorError(PredecessorRefusal.INVALID_PIN)


@dataclass(frozen=True)
class VerifiedPredecessor:
    source: ImmutablePredecessor


ERP_ACCOUNTING_FACT_PREDECESSOR = ImmutablePredecessor(
    commit="f3a18e7ff2d89821eb410760bd326253fb5c36af",
    tree="0b9f9efe2b4d780bd5530c89828ee8ca41333504",
)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    # Never relay Git transport diagnostics: an origin URL can contain auth.
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def verify_predecessor(
    *, repository: Path, source: ImmutablePredecessor
) -> VerifiedPredecessor:
    """Verify a local exact commit and tree without acquiring or moving refs."""
    kind = _git(repository, "cat-file", "-t", source.commit)
    if kind.returncode != 0:
        raise PredecessorError(PredecessorRefusal.MISSING_OBJECT)
    if kind.stdout.strip() != "commit":
        raise PredecessorError(PredecessorRefusal.NOT_COMMIT)
    tree = _git(repository, "rev-parse", "--verify", f"{source.commit}^{{tree}}")
    if tree.returncode != 0 or tree.stdout.strip() != source.tree:
        raise PredecessorError(PredecessorRefusal.WRONG_TREE)
    return VerifiedPredecessor(source=source)


def acquire_predecessor(
    *, repository: Path, source: ImmutablePredecessor
) -> VerifiedPredecessor:
    """Fetch only a missing exact SHA from origin, then verify its pinned tree."""
    try:
        return verify_predecessor(repository=repository, source=source)
    except PredecessorError as exc:
        if exc.code is not PredecessorRefusal.MISSING_OBJECT:
            raise
    fetched = _git(
        repository,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "origin",
        source.commit,
    )
    if fetched.returncode != 0:
        raise PredecessorError(PredecessorRefusal.FETCH_FAILED)
    return verify_predecessor(repository=repository, source=source)


def main() -> int:
    try:
        verified = acquire_predecessor(
            repository=Path(__file__).resolve().parents[2],
            source=ERP_ACCOUNTING_FACT_PREDECESSOR,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(
            "Immutable predecessor refused: acquisition unavailable.", file=sys.stderr
        )
        return 1
    except PredecessorError as exc:
        print(f"Immutable predecessor refused: {exc.code.value}.", file=sys.stderr)
        return 1
    print(
        f"Immutable predecessor verified: commit={verified.source.commit} "
        f"tree={verified.source.tree}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
