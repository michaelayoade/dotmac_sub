"""Shared analysis for the local engine/session construction ratchet.

Sub pins Kernel `a94` but deliberately keeps its own database/session
authority (`app/db.py`). Before that boundary can ever move to
`dotmac_kernel.session_runtime.DatabaseRuntime`, every place that builds a
SQLAlchemy `Engine` or ORM `Session` outside the one canonical factory
(`app.db.SessionLocal`, itself built from `app.db.get_engine`) needs to be
named, so a shared runtime seam is not designed against an incomplete count.

This module finds those construction sites with `ast`, not text search, so a
comment or docstring that merely mentions ``sessionmaker`` or ``create_engine``
is never mistaken for a real call site (see
``test_scanner_ignores_comment_and_docstring_mentions`` for the proof, and
``app/models/auth.py``'s unrelated ``class Session(Base):`` domain model for a
real-repository near-miss the AST walk already has to get right).

## Historical residue is excluded from the LIVE surface

A handful of the files this sweep finds are one-time migration/preflight
scripts for a subscriber/ticketing system integration that has since been
fully decommissioned. They still construct a real `Engine` against a second,
now-unreachable external database — a `DatabaseRuntime` seam has nothing to
preserve there, because there is no live requirement left to preserve; only
retirement residue remains. `production_counts_by_file` therefore excludes
any file already recorded in the repository's OWN authoritative ledger for
that decommissioned integration's surface
(`tests/architecture/crm_vocabulary_baseline.txt`, enforced by
`tests/architecture/test_crm_vocabulary_freeze.py`) — a cross-reference to
that existing ledger, not a second, hand-maintained path list here, so the
exclusion tracks that ledger automatically rather than drifting from it. The
excluded set is never named path-by-path outside that ledger and this
module: see `historical_residue_paths` for exactly where.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.crm_vocabulary import (
    surface_paths as _decommissioned_integration_surface,
)
from tests.architecture.source_index import python_ast, python_files

#: Canonical SQLAlchemy names whose invocation constructs a new engine or a
#: new, independently-lifecycled ORM session. `engine_from_config` is
#: Alembic's constructor, not `create_engine`, but it is the same primitive
#: for this inventory's purpose. Async equivalents are tracked even though
#: today's sweep finds zero uses, so the ratchet catches the first one.
CONSTRUCTOR_NAMES: frozenset[str] = frozenset(
    {
        "create_engine",
        "create_async_engine",
        "engine_from_config",
        "sessionmaker",
        "async_sessionmaker",
        "Session",
        "AsyncSession",
    }
)

#: Only names imported from one of these modules (directly, or via
#: `import sqlalchemy[...] as x` + attribute access) count as the real
#: SQLAlchemy primitive. This is what keeps an unrelated class named
#: ``Session`` (e.g. a domain "auth session" model) from ever matching a bare
#: `Session(...)` call written elsewhere in the same file.
SQLALCHEMY_MODULE_PREFIXES: tuple[str, ...] = ("sqlalchemy",)

#: Production/operational surfaces this ratchet holds exactly. `tests/` is
#: excluded here — deliberately, not silently: test fixtures construct one
#: ad hoc SQLite (or Postgres, for integration) engine per test by design, so
#: a per-file exact count would churn with test authorship and not with the
#: runtime-readiness question this inventory answers. `tests/` construction
#: is still swept and reported in `test_test_fixture_engine_family_is_swept`
#: as an aggregate, informational count.
PRODUCTION_ROOTS: tuple[str, ...] = ("app", "scripts", "alembic")

TEST_ROOTS: tuple[str, ...] = ("tests",)


def _tracked_aliases(tree: ast.Module) -> dict[str, str]:
    """Map a local name to the canonical SQLAlchemy constructor it refers to.

    Only names actually imported from a `sqlalchemy*` module are tracked, so
    `from foo import Session` (some unrelated `Session`) is never tracked, and
    a bare `Session(...)` call in that file cannot match.
    """

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not any(
            module == prefix or module.startswith(prefix + ".")
            for prefix in SQLALCHEMY_MODULE_PREFIXES
        ):
            continue
        for alias in node.names:
            if alias.name in CONSTRUCTOR_NAMES:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def construction_sites(tree: ast.Module) -> list[tuple[int, str]]:
    """Return `(lineno, canonical_name)` for every construction call in `tree`.

    An attribute call (`sa.create_engine(...)`, `orm.Session(...)`) is matched
    by attribute name alone — the attribute access already disambiguates it
    from an unrelated bare name. A bare call (`create_engine(...)`,
    `Session(...)`) is matched only against names this module actually
    imported from `sqlalchemy*` (see `_tracked_aliases`), which is what keeps
    a domain model's `class Session(Base):` and any comment mentioning these
    words from ever being counted.
    """

    aliases = _tracked_aliases(tree)
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            sites.append((node.lineno, aliases[func.id]))
        elif isinstance(func, ast.Attribute) and func.attr in CONSTRUCTOR_NAMES:
            sites.append((node.lineno, func.attr))
    return sites


def sites_for(path: Path) -> list[tuple[int, str]]:
    return sorted(construction_sites(python_ast(path)))


def counts_by_file(roots: tuple[str, ...]) -> dict[str, int]:
    """Current construction-site count per file, keyed by repo-relative path."""

    counts: dict[str, int] = {}
    for root in roots:
        for path in python_files(Path(root)):
            found = sites_for(path)
            if found:
                counts[path.as_posix()] = len(found)
    return counts


def historical_residue_paths() -> frozenset[str]:
    """Production files excluded from the LIVE ratchet as retirement residue.

    A cross-reference to the repository's own frozen surface for a fully
    decommissioned integration, not a second copy of that list — see the
    module docstring. A file only ever needs to be named once, in the
    ledger that owns the retirement decision for it; this function reads
    that ledger rather than repeating it.
    """

    return _decommissioned_integration_surface()


def raw_production_counts_by_file() -> dict[str, int]:
    """Every production construction site this sweep finds, unfiltered —
    live surface and historical residue together. Used only to prove the
    exclusion mechanism actually removes something (see
    `test_historical_residue_is_excluded_from_the_live_ratchet_but_still_swept`)
    rather than vacuously matching an already-empty set."""

    return counts_by_file(PRODUCTION_ROOTS)


def production_counts_by_file() -> dict[str, int]:
    """LIVE production construction-site counts: the mechanical sweep with
    historical residue excluded. This is what a shared `DatabaseRuntime`
    would actually need to account for."""

    residue = historical_residue_paths()
    return {
        path: count
        for path, count in raw_production_counts_by_file().items()
        if path not in residue
    }


def production_residue_counts_by_file() -> dict[str, int]:
    """The excluded counterpart of `production_counts_by_file`: historical
    residue construction sites this sweep found but does not treat as a
    live requirement. Informational only — never baselined per-file."""

    residue = historical_residue_paths()
    return {
        path: count
        for path, count in raw_production_counts_by_file().items()
        if path in residue
    }


def test_fixture_total_count() -> int:
    return sum(counts_by_file(TEST_ROOTS).values())
