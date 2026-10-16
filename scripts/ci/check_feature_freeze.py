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
  external surface is feature-shaped until proven corrective);
- a newly added template file under ``templates/`` (rule ``new-template``);
- a PR whose added lines in ``app/`` + ``templates/`` (tests, migrations,
  lockfiles and minified assets excluded) exceed ``SIZE_THRESHOLD_LINES``
  (rule ``size-threshold``). These rules apply whatever the PR title says: a
  ``fix:`` / ``chore:`` / ``refactor:`` label does not make a feature-sized
  change a correction.

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

#: Added lines in SIZE_COUNTED_PATTERNS above this make a PR feature-sized
#: regardless of its title. Override with FREEZE_SIZE_THRESHOLD or
#: ``--size-threshold``.
SIZE_THRESHOLD_LINES = 400
#: static/ is counted for JavaScript only (not CSS or images); vendored and
#: minified JS is excluded below.
SIZE_COUNTED_PATTERNS = (
    "app/**",
    "templates/**",
    "static/**/*.js",
    "app/static/**/*.js",
)
SIZE_EXCLUDED_PATTERNS = (
    "tests/**",
    "alembic/**",
    "**/tests/**",
    "*.lock",
    "**/*.lock",
    "**/package-lock.json",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.md",
    "static/vendor/**",
    "static/**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
)
NEW_TEMPLATE_PATTERNS = ("templates/**",)

RULE_FEAT_TITLE = "feat-title"
RULE_OPENAPI = "openapi-manifest"
RULE_TRANSITIONAL = "new-transitional-tool"
RULE_MIGRATION = "new-migration"
RULE_NEW_MODULE = "new-python-module"
RULE_NEW_TEMPLATE = "new-template"
RULE_SIZE = "size-threshold"
RULE_UNCOUNTABLE = "uncountable-diff"

#: approved-growth entries may also cover findings from these rules (a
#: genuine fix adding a partial need not invent a Starter contract).
GROWTH_COVERABLE_RULES = (RULE_NEW_TEMPLATE, RULE_SIZE)

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

EXCEPTION_KINDS = {
    "approved-growth",
    "feature-exception",
    "schema",
    "transitional-tool",
}


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


#: -z: raw NUL-separated paths (git quotes non-ASCII otherwise);
#: --no-renames: a rename is a delete + add so path rules see the destination.
_DIFF_FLAGS = ("diff", "-z", "--no-renames")
#: numstat count for binary / ``-diff`` (undiffable) files.
UNCOUNTABLE = -1


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

    result = _git(*_DIFF_FLAGS, "--name-status", f"{base}...{head}")
    if result.returncode != 0:
        _deepen_base(base)
        result = _git(*_DIFF_FLAGS, "--name-status", f"{base}...{head}")
    if result.returncode != 0:
        print(
            "::warning::feature-freeze: merge-base unavailable for "
            f"'{base}...{head}'; falling back to two-dot diff"
        )
        result = _git(*_DIFF_FLAGS, "--name-status", base, head)
        result.check_returncode()
    return parse_name_status_z(result.stdout)


def parse_name_status_z(output: str) -> list[tuple[str, str]]:
    """Parse ``git diff -z --name-status --no-renames`` (NUL separated)."""

    fields = output.split("\0")
    pairs: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(fields):
        status, path = fields[i].strip(), fields[i + 1]
        i += 2
        if status:
            pairs.append((status, path))
    return pairs


def _numstat(base: str, head: str) -> dict[str, int]:
    """{path: added lines}; UNCOUNTABLE (-1) when git reports ``-``."""

    result = _git(*_DIFF_FLAGS, "--numstat", f"{base}...{head}")
    if result.returncode != 0:
        result = _git(*_DIFF_FLAGS, "--numstat", base, head)
        result.check_returncode()
    return parse_numstat_z(result.stdout)


def parse_numstat_z(output: str) -> dict[str, int]:
    """Parse ``git diff -z --numstat --no-renames`` (NUL separated)."""

    added: dict[str, int] = {}
    for record in output.split("\0"):
        parts = record.lstrip("\n").split("\t", 2)
        if len(parts) < 3:
            continue
        if parts[0].isdigit():
            added[parts[2]] = int(parts[0])
        elif parts[0] == "-":
            added[parts[2]] = UNCOUNTABLE
    return added


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


def classify_detailed(
    changes: list[tuple[str, str]],
    title: str,
    added_lines: dict[str, int] | None = None,
    size_threshold: int = SIZE_THRESHOLD_LINES,
) -> list[tuple[str, str, str]]:
    """Return (kind, path, rule) feature-shaped findings for a change set.

    ``added_lines`` maps path -> added line count; when omitted the size rule
    is not evaluated. Rules apply independently of the PR title type.
    """

    findings: list[tuple[str, str, str]] = []
    if FEAT_TITLE_RE.match(title.strip()):
        findings.append(
            ("feature-exception", f"PR title: {title.strip()}", RULE_FEAT_TITLE)
        )
    for status, path in changes:
        if path == OPENAPI_MANIFEST and status != "D":
            findings.append(("feature-exception", path, RULE_OPENAPI))
            continue
        if status != "A":
            continue
        if _matches(path, ALWAYS_ALLOWED_PATTERNS):
            continue
        if _matches(path, NEW_TRANSITIONAL_PATTERNS):
            findings.append(("transitional-tool", path, RULE_TRANSITIONAL))
        elif _matches(path, NEW_MIGRATION_PATTERNS):
            findings.append(("schema", path, RULE_MIGRATION))
        elif _matches(path, NEW_MODULE_PATTERNS):
            findings.append(("feature-exception", path, RULE_NEW_MODULE))
        elif _matches(path, NEW_TEMPLATE_PATTERNS):
            findings.append(("feature-exception", path, RULE_NEW_TEMPLATE))

    if added_lines is not None:
        counted = {
            path: n
            for path, n in added_lines.items()
            if n > 0
            and _matches(path, SIZE_COUNTED_PATTERNS)
            and not _matches(path, SIZE_EXCLUDED_PATTERNS)
        }
        for path, n in sorted(added_lines.items()):
            if (
                n == UNCOUNTABLE
                and _matches(path, SIZE_COUNTED_PATTERNS)
                and not _matches(path, SIZE_EXCLUDED_PATTERNS)
            ):
                findings.append(
                    (
                        "feature-exception",
                        path,
                        f"{RULE_UNCOUNTABLE}: git cannot count lines "
                        "(binary or -diff attribute)",
                    )
                )
        total = sum(counted.values())
        if total > size_threshold:
            already = {path for _, path, _ in findings}
            for path, n in sorted(counted.items(), key=lambda kv: -kv[1]):
                if path in already:
                    continue
                findings.append(
                    (
                        "feature-exception",
                        path,
                        f"{RULE_SIZE}: PR adds {total} lines in app/+templates/ "
                        f"(limit {size_threshold}); this file +{n}",
                    )
                )
    return findings


def classify(
    changes: list[tuple[str, str]],
    title: str,
    added_lines: dict[str, int] | None = None,
    size_threshold: int = SIZE_THRESHOLD_LINES,
) -> list[tuple[str, str]]:
    """Return (kind, path) feature-shaped findings for a change set."""

    return [
        (kind, path)
        for kind, path, _ in classify_detailed(
            changes, title, added_lines, size_threshold
        )
    ]


def load_exceptions(
    root: Path, exceptions_file: Path | None = None
) -> list[dict[str, object]]:
    path = exceptions_file or root / EXCEPTIONS_PATH
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("exceptions", [])
    return entries if isinstance(entries, list) else []


def exception_covers(
    entries: list[dict[str, object]], kind: str, path: str, rule: str = ""
) -> bool:
    growth_ok = kind == "feature-exception" and rule.startswith(GROWTH_COVERABLE_RULES)
    for entry in entries:
        if entry.get("kind") != kind and not (
            growth_ok and entry.get("kind") == "approved-growth"
        ):
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
    parser.add_argument(
        "--size-threshold",
        type=int,
        default=int(os.getenv("FREEZE_SIZE_THRESHOLD", SIZE_THRESHOLD_LINES)),
        help="Max added lines in app/ + templates/ before an exception is "
        f"required (default {SIZE_THRESHOLD_LINES}).",
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=None,
        help="Exceptions JSON to trust (CI passes the base branch's copy; "
        f"default {EXCEPTIONS_PATH} in the working tree).",
    )
    args = parser.parse_args()

    pr = _event_pull_request()
    title = args.title or str(pr.get("title") or "")
    changes = _name_status(args.base, args.head)
    findings = classify_detailed(
        changes, title, _numstat(args.base, args.head), args.size_threshold
    )

    root = Path.cwd()
    entries = load_exceptions(root, args.exceptions)
    problems = validate_exceptions(entries)
    for problem in problems:
        print(f"::error::feature-freeze: {problem}")
    if problems:
        return 1

    uncovered = [
        (kind, path, rule)
        for kind, path, rule in findings
        if not exception_covers(entries, kind, path, rule)
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
    for kind, path, rule in uncovered:
        print(f"- [{kind}] rule={rule} file={path}")
    print(
        "Build new capability in Starter and consume it as a pinned "
        "package, or record Michael's exception (CODEOWNERS-reviewed)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
