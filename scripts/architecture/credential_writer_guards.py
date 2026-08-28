"""Structural detectors for who assigns a credential's RADIUS profile columns.

Ledger row ``COL-R5`` (Starter's ``docs/inventories/commercial-retirement-ledger.md``)
retires collections' direct credential writes in favour of a typed consequence
request. ``app.services.collections_authority`` holds the declarations; this
module supplies the evidence they are compared against.

The unit counted is an ASSIGNMENT, not a reference. Collections still reads
both columns to build its preview, and a decision input is not a write.

The detectors take an explicit root so they can be pointed at a planted tree
and shown to fail — ADR-0018 decision 5. A guard whose sensitivity is never
demonstrated cannot distinguish a clean region from a detector that stopped
looking.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

#: The columns whose assignment moves a credential's RADIUS profile state.
CREDENTIAL_PROFILE_ATTRS = frozenset(
    {
        "radius_profile_id",
        "pre_throttle_radius_profile_id",
    }
)


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _assignment_targets(tree: ast.Module) -> list[ast.Attribute]:
    """Every attribute an assignment statement writes to."""

    targets: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            candidates: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            candidates = [node.target]
        else:
            continue
        for candidate in candidates:
            if isinstance(candidate, ast.Attribute):
                targets.append(candidate)
            elif isinstance(candidate, ast.Tuple | ast.List):
                targets.extend(
                    element
                    for element in candidate.elts
                    if isinstance(element, ast.Attribute)
                )
    return targets


def credential_profile_write_sites(root: Path | None = None) -> dict[str, int]:
    """Count RADIUS-profile assignments per file, relative to the project root.

    Files with no assignment are absent rather than zero, so a shrink-only
    baseline shrinks by losing lines.
    """

    base = root if root is not None else APP_DIR
    anchor = PROJECT_ROOT if root is None else base
    counts: dict[str, int] = {}
    for path in _python_files(base):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - defensive
            continue
        hits = sum(
            1
            for target in _assignment_targets(tree)
            if target.attr in CREDENTIAL_PROFILE_ATTRS
        )
        if hits:
            counts[path.relative_to(anchor).as_posix()] = hits
    return counts


def symbol_reference_sites(
    symbols: frozenset[str], roots: tuple[Path, ...]
) -> dict[str, int]:
    """Count textual references to each symbol across the given roots.

    Textual on purpose: a retired symbol must be gone from tests, comments and
    string literals too, not merely unreachable from production code.
    """

    counts: dict[str, int] = {}
    for root in roots:
        if not root.exists():  # pragma: no cover - defensive
            continue
        for path in _python_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover - defensive
                continue
            hits = sum(text.count(symbol) for symbol in symbols)
            if hits:
                counts[_label(path, root)] = hits
    return counts


def _label(path: Path, root: Path) -> str:
    """Name a file relative to the project, or to the root it was found under.

    A planted tree lives outside the project, so ``relative_to(PROJECT_ROOT)``
    would raise on exactly the input a sensitivity proof supplies.
    """

    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.relative_to(root).as_posix()
