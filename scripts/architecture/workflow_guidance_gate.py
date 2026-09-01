"""Fail when an Admin workflow changes without its guidance contract changing."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import PurePosixPath

GUIDANCE_SOURCE = PurePosixPath("app/services/admin_workflow_guidance.py")
WORKFLOW_ROOTS = (PurePosixPath("app/web/admin"),)


def changed_paths(base: str) -> tuple[PurePosixPath, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base, "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(PurePosixPath(line) for line in result.stdout.splitlines() if line)


def validation_errors(paths: tuple[PurePosixPath, ...]) -> tuple[str, ...]:
    changed_workflow = any(
        any(path.is_relative_to(root) for root in WORKFLOW_ROOTS) for path in paths
    )
    changed_guidance = GUIDANCE_SOURCE in paths
    if changed_workflow and not changed_guidance:
        return (
            "Admin workflow code changed without updating "
            "app/services/admin_workflow_guidance.py. Update the linked plain-language guide in the same PR.",
        )
    return ()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", required=True, help="Git base revision to compare with HEAD"
    )
    args = parser.parse_args()
    errors = validation_errors(changed_paths(args.base))
    if errors:
        print("Workflow guidance gate failed:", file=sys.stderr)
        print(*errors, sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
