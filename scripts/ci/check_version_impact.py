#!/usr/bin/env python3
"""Validate that release-impacting PRs declare a version impact."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

VERSION_LABELS = {
    "version:major",
    "version:minor",
    "version:patch",
    "version:none",
}

RELEASE_PATTERNS = (
    "app/**",
    "templates/**",
    "static/**",
    "mobile/**",
    "alembic/**",
    "docker/**",
    "Dockerfile",
    "docker-compose*.yml",
    "brand.json",
    "pyproject.toml",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "scripts/**",
)

NON_RELEASE_PATTERNS = (
    ".github/**",
    "docs/**",
    "scratchpad/**",
    "tests/**",
    "scripts/ci/**",
    "mobile/test/**",
    "mobile/test_live/**",
    "*.md",
    "**/*.md",
)

NONE_JUSTIFICATION_RE = re.compile(
    r"version impact:\s*none\s+because\s+\S", re.IGNORECASE
)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _git_diff_names(spec: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "diff", "--name-only", spec],
        capture_output=True,
        text=True,
    )


def _deepen_base(base: str) -> None:
    """Best-effort: fetch the base branch's history so a merge-base exists.

    On a shallow CI checkout ``base`` (e.g. ``origin/main``) may be a single
    commit with no ancestors, which makes the three-dot diff fail. Derive the
    remote/branch from ``base`` and deepen it.
    """
    remote, _, branch = base.partition("/")
    if not remote or not branch:
        return
    subprocess.run(
        ["git", "fetch", "--no-tags", remote, branch],
        capture_output=True,
        text=True,
    )


def _changed_files(base: str, head: str) -> list[str]:
    # Three-dot (merge-base) is the correct "files this PR introduces" semantic.
    result = _git_diff_names(f"{base}...{head}")
    if result.returncode != 0:
        # Likely a shallow base tip with no reachable merge-base — deepen & retry.
        _deepen_base(base)
        result = _git_diff_names(f"{base}...{head}")
    if result.returncode != 0:
        # Last resort: two-dot diff (tip-vs-tip) so the gate still runs instead
        # of crashing. It may over-report files changed on base, which only ever
        # makes the check stricter (require a label), never laxer.
        print(
            "::warning::version-impact: merge-base unavailable for "
            f"'{base}...{head}'; falling back to two-dot diff"
        )
        result = _git_diff_names(f"{base} {head}")
        result.check_returncode()
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _event_pull_request() -> dict[str, object]:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return {}

    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    pr = event.get("pull_request")
    return pr if isinstance(pr, dict) else {}


def _version_labels(pr: dict[str, object]) -> list[str]:
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return []

    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            name = str(label["name"])
            if name in VERSION_LABELS:
                names.append(name)
    return sorted(names)


def _has_none_justification(pr: dict[str, object]) -> bool:
    body = pr.get("body")
    return bool(isinstance(body, str) and NONE_JUSTIFICATION_RE.search(body))


def _release_relevant(files: list[str]) -> list[str]:
    relevant: list[str] = []
    for path in files:
        if _matches(path, NON_RELEASE_PATTERNS):
            continue
        if _matches(path, RELEASE_PATTERNS):
            relevant.append(path)
    return relevant


def _print_failure(message: str) -> None:
    print(f"::error::{message}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require version impact labels for deployable PR changes."
    )
    parser.add_argument("--base", required=True, help="Base git ref for the PR.")
    parser.add_argument("--head", default="HEAD", help="Head git ref for the PR.")
    args = parser.parse_args()

    files = _changed_files(args.base, args.head)
    pr = _event_pull_request()
    labels = _version_labels(pr)
    release_files = _release_relevant(files)

    if len(labels) > 1:
        _print_failure(
            "Use exactly one version impact label: "
            "version:major, version:minor, version:patch, or version:none."
        )
        print(f"Found labels: {', '.join(labels)}")
        return 1

    if not release_files:
        print("No deployable release-impacting files changed.")
        return 0

    print("Release-impacting files detected:")
    for path in release_files:
        print(f"- {path}")

    if not labels:
        _print_failure(
            "Deployable changes require one version impact label: "
            "version:major, version:minor, version:patch, or version:none."
        )
        return 1

    label = labels[0]
    print(f"Version impact label: {label}")

    if label == "version:none" and not _has_none_justification(pr):
        _print_failure(
            "PRs labeled version:none must include "
            "'Version impact: none because ...' in the PR body."
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
