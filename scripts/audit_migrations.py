#!/usr/bin/env python3
"""Audit Alembic revisions for basic graph hygiene."""

from __future__ import annotations

import ast
from collections.abc import Iterable
import sys
from pathlib import Path

VERSIONS_DIR = Path("alembic/versions")


def _read_constant_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _read_down_revisions(node: ast.AST | None) -> tuple[str, ...] | None:
    value = _read_constant_string(node)
    if value is not None:
        return (value,)
    if isinstance(node, (ast.Tuple, ast.List)):
        items: list[str] = []
        for elt in node.elts:
            item = _read_constant_string(elt)
            if item is None:
                return None
            items.append(item)
        return tuple(items)
    return None


def _read_revision(path: Path) -> tuple[str | None, tuple[str, ...] | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    revision = None
    down_revisions = None

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "revision":
                revision = _read_constant_string(node.value)
            if target.id == "down_revision":
                down_revisions = _read_down_revisions(node.value)

    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name):
            continue
        if node.target.id == "revision":
            revision = _read_constant_string(node.value)
        if node.target.id == "down_revision":
            down_revisions = _read_down_revisions(node.value)

    return revision, down_revisions


def _iter_missing_parents(
    revision: str,
    down_revisions: tuple[str, ...] | None,
    known_revisions: set[str],
) -> Iterable[str]:
    if down_revisions is None:
        return ()
    return (
        parent
        for parent in down_revisions
        if parent is not None and parent not in known_revisions
    )


def main() -> int:
    issues: list[str] = []
    seen_revisions: dict[str, Path] = {}
    parents_by_revision: dict[str, tuple[str, ...] | None] = {}

    for path in sorted(VERSIONS_DIR.glob("*.py")):
        revision, down_revisions = _read_revision(path)
        if revision is None:
            issues.append(f"{path}: missing revision")
            continue

        if revision in seen_revisions:
            issues.append(
                f"duplicate revision {revision}: {seen_revisions[revision]} and {path}"
            )
        else:
            seen_revisions[revision] = path
            parents_by_revision[revision] = down_revisions

        if not path.stem.startswith(revision):
            issues.append(
                f"{path}: filename {path.stem!r} does not start with revision {revision!r}"
            )

    known_revisions = set(seen_revisions)
    child_revisions = {
        parent
        for down_revisions in parents_by_revision.values()
        if down_revisions is not None
        for parent in down_revisions
    }

    for revision, down_revisions in sorted(parents_by_revision.items()):
        if revision == "799a0ecebdd4":
            continue
        if down_revisions is None:
            issues.append(f"{seen_revisions[revision]}: missing down_revision")
            continue
        for parent in _iter_missing_parents(revision, down_revisions, known_revisions):
            issues.append(
                f"{seen_revisions[revision]}: missing parent revision {parent!r}"
            )

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1

    heads = sorted(known_revisions - child_revisions)
    print(f"ok: audited {len(seen_revisions)} revisions, heads={len(heads)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
