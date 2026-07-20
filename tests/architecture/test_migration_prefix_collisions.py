"""Guard: no new migration files may share a numeric prefix.

Six times in three days (2026-07-18..20) a parallel branch claimed the next
migration number while main outran it, colliding at land or deploy time
(multi-head alembic, or "can't locate revision" on a database that applied
the other file). Two files with one prefix is the collision's first visible
symptom — fail it at CI instead of at deploy. Historical duplicates are
frozen in the shrink-only baseline.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERSIONS = REPO_ROOT / "alembic" / "versions"
BASELINE = Path(__file__).parent / "migration_prefix_collision_baseline.txt"


def _numeric_prefixes() -> Counter:
    counts: Counter = Counter()
    for path in VERSIONS.glob("*.py"):
        match = re.match(r"^([0-9]+)_", path.name)
        if match:
            counts[match.group(1)] += 1
    return counts


def _baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_no_new_duplicate_migration_prefixes():
    duplicated = {p for p, n in _numeric_prefixes().items() if n > 1}
    new = duplicated - _baseline()
    assert not new, (
        f"Migration prefix collision: {sorted(new)} is used by more than one "
        "file. Renumber the newer migration onto the next free number and "
        "re-parent it on the current head; never add to the baseline."
    )


def test_baseline_only_shrinks():
    duplicated = {p for p, n in _numeric_prefixes().items() if n > 1}
    stale = _baseline() - duplicated
    assert not stale, (
        f"Baseline entries no longer duplicated: {sorted(stale)} — remove them "
        "from migration_prefix_collision_baseline.txt (shrink-only)."
    )
