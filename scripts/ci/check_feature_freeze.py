#!/usr/bin/env python3
"""Enforce the Sub feature freeze: corrections only, no new features.

Sub is migrating to a thin client of the build-once Starter platform
(ADR 0020). New capability is built once in Starter and consumed here as a
pinned package; Sub itself accepts corrections only — bug/security fixes,
freeze and retirement changes, tests, docs, package pins and composition
wiring. This gate makes that mechanical so it does not depend on reviewer
vigilance. See ``docs/FEATURE_FREEZE.md`` for the full rules.

Feature-shaped change (requires an approved exception entry in
``docs/feature_freeze_exceptions.json``, a CODEOWNERS-protected file):

- a PR title declaring ``feat:`` / ``feat(...)``;
- a newly added Python module anywhere under ``app/``;
- a newly added Alembic migration (schema change is new capability until
  proven corrective);
- a newly added transitional billing tool (must also be registered with a
  deletion trigger);
- any modification of the pinned OpenAPI contract manifest (a changed
  external surface is feature-shaped until proven corrective).

Modifications and deletions of existing files, and additions under
``tests/``, ``docs/`` or ``.github/``, are corrections-shaped and pass.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

EXCEPTIONS_PATH = Path("docs/feature_freeze_exceptions.json")
OPENAPI_MANIFEST = "tests/architecture/openapi_contract_surface.json"

FEAT_TITLE_RE = re.compile(r"^feat(\([^)]*\))?(!)?:", re.IGNORECASE)

#: Added files matching these patterns are new capability in Sub.
NEW_MODULE_PATTERNS = ("app/*.py", "app/**/*.py")
NEW_MIGRATION_PATTERNS = ("alembic/versions/*.py",)
NEW_TRANSITIONAL_PATTERNS = (
    "app/services/billing/opening_*.py",
    "app/services/billing_numbering_*.py",
    "scripts/migration/billing_opening_*.py",
    "scripts/migration/billing_numbering_*.py",
    "scripts/billing/*.py",
    "scripts/billing/**/*.py",
)

#: Additions here never count as features.
ALWAYS_ALLOWED_PATTERNS = (
    "tests/**",
    "docs/**",
    ".github/**",
    "*.md",
    "**/*.md",
)

EXCEPTION_KINDS = {"feature-exception", "schema", "transitional-tool"}


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _git(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *argv], capture_output=True, text=True)


def _deepen_base(base: str) -> None:
    """Best-effort base deepen, mirroring check_version_impact.py."""

    remote, _, branch = base.partition("/")
    if not remote or not branch:
        return
    _git("fetch", "--no-tags", remote, branch)


def _name_status(base: str, head: str) -> list[tuple[str, str]]:
    """(status, path) pairs this PR introduces (three-dot semantics)."""

    result = _git("diff", "--name-status", f"{base}...{head}")
    if result.returncode != 0:
        _deepen_base(base)
        result = _git("diff", "--name-status", f"{base}...{head}")
    if result.returncode != 0:
        print(
            "::warning::feature-freeze: merge-base unavailable for "
            f"'{base}...{head}'; falling back to two-dot diff"
        )
        result = _git("diff", "--name-status", base, head)
        result.check_returncode()
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        # Renames carry two paths; the destination is what ships.
        path = parts[-1].strip()
        pairs.append((status, path))
    return pairs


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


def classify(changes: list[tuple[str, str]], title: str) -> list[tuple[str, str]]:
    """Return (kind, path) feature-shaped findings for a change set."""

    findings: list[tuple[str, str]] = []
    if FEAT_TITLE_RE.match(title.strip()):
        findings.append(("feature-exception", f"PR title: {title.strip()}"))
    for status, path in changes:
        if path == OPENAPI_MANIFEST and status != "D":
            findings.append(("feature-exception", path))
            continue
        if status != "A":
            continue
        if _matches(path, ALWAYS_ALLOWED_PATTERNS):
            continue
        if _matches(path, NEW_TRANSITIONAL_PATTERNS):
            findings.append(("transitional-tool", path))
        elif _matches(path, NEW_MIGRATION_PATTERNS):
            findings.append(("schema", path))
        elif _matches(path, NEW_MODULE_PATTERNS):
            findings.append(("feature-exception", path))
    return findings


def load_exceptions(root: Path) -> list[dict[str, object]]:
    path = root / EXCEPTIONS_PATH
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("exceptions", [])
    return entries if isinstance(entries, list) else []


def exception_covers(entries: list[dict[str, object]], kind: str, path: str) -> bool:
    for entry in entries:
        if entry.get("kind") != kind:
            continue
        if entry.get("approved_by") != "michaelayoade":
            continue
        patterns = entry.get("paths")
        if not isinstance(patterns, list):
            continue
        # The feat-title finding carries no path; any well-formed
        # feature-exception entry with a reason covers the declaration —
        # the individual paths are still checked on their own findings.
        if path.startswith("PR title:"):
            if entry.get("reason"):
                return True
            continue
        if any(fnmatch.fnmatch(path, str(p)) for p in patterns):
            if kind == "transitional-tool" and not entry.get("deletion_trigger"):
                continue
            return True
    return False


def validate_exceptions(entries: list[dict[str, object]]) -> list[str]:
    problems: list[str] = []
    for index, entry in enumerate(entries):
        where = f"exceptions[{index}]"
        if entry.get("kind") not in EXCEPTION_KINDS:
            problems.append(f"{where}: kind must be one of {sorted(EXCEPTION_KINDS)}")
        if not entry.get("id"):
            problems.append(f"{where}: missing id")
        if not entry.get("reason"):
            problems.append(f"{where}: missing reason")
        if entry.get("approved_by") != "michaelayoade":
            problems.append(f"{where}: approved_by must be michaelayoade")
        if entry.get("kind") == "transitional-tool" and not entry.get(
            "deletion_trigger"
        ):
            problems.append(f"{where}: transitional-tool needs deletion_trigger")
        if entry.get("kind") == "feature-exception" and not entry.get(
            "starter_contract"
        ):
            problems.append(
                f"{where}: feature-exception must name the Starter contract "
                "it consumes (build capabilities in Starter, not Sub)"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail PRs that add features to Sub during the freeze."
    )
    parser.add_argument("--base", required=True, help="Base git ref.")
    parser.add_argument("--head", default="HEAD", help="Head git ref.")
    parser.add_argument(
        "--title",
        default="",
        help="PR title (defaults to the GitHub event payload).",
    )
    args = parser.parse_args()

    pr = _event_pull_request()
    title = args.title or str(pr.get("title") or "")
    changes = _name_status(args.base, args.head)
    findings = classify(changes, title)

    root = Path.cwd()
    entries = load_exceptions(root)
    problems = validate_exceptions(entries)
    for problem in problems:
        print(f"::error::feature-freeze: {problem}")
    if problems:
        return 1

    uncovered = [
        (kind, path)
        for kind, path in findings
        if not exception_covers(entries, kind, path)
    ]
    if not uncovered:
        print(
            "Feature freeze: no unapproved feature-shaped change "
            f"({len(findings)} finding(s), all covered by exceptions)."
        )
        return 0

    print(
        "::error::feature-freeze: Sub accepts corrections only while it "
        "migrates to a thin client of Starter (docs/FEATURE_FREEZE.md). "
        "These changes are feature-shaped and have no approved exception "
        "in docs/feature_freeze_exceptions.json:"
    )
    for kind, path in uncovered:
        print(f"- [{kind}] {path}")
    print(
        "Build new capability in Starter and consume it as a pinned "
        "package, or record Michael's exception (CODEOWNERS-reviewed)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
