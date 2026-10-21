"""Pure predecessor policy for the deploy executor's locked observation."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class PredecessorQuery:
    expected: str
    repository: str
    previous: str
    observation: str


class PredecessorOutcome(str, Enum):
    MATCH = "from_image_match"
    INVALID = "invalid_from_image_precondition"
    MISMATCH = "from_image_mismatch"


def check_predecessor(query: PredecessorQuery) -> PredecessorOutcome:
    """Require an exact immutable pin and running-image observation."""
    if (
        re.fullmatch(
            re.escape(query.repository) + r"@sha256:[0-9a-f]{64}", query.expected
        )
        is None
    ):
        return PredecessorOutcome.INVALID
    if (
        query.previous != query.expected
        or query.observation != f"true|{query.expected}"
    ):
        return PredecessorOutcome.MISMATCH
    return PredecessorOutcome.MATCH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("expected", "repository", "previous", "observation"):
        parser.add_argument(f"--{field}", required=True)
    args = parser.parse_args()
    outcome = check_predecessor(
        PredecessorQuery(
            expected=args.expected,
            repository=args.repository,
            previous=args.previous,
            observation=args.observation,
        )
    )
    if outcome is not PredecessorOutcome.MATCH:
        print(f"DEPLOY REFUSED: {outcome.value}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
