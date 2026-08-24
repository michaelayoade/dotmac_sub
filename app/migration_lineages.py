"""Which module lineages this assembly composes, and how to make them visible.

Composing a Starter module means running its migrations from inside its
installed distribution. Alembic will not find them on its own, and the way it
fails is silent: Sub's own revisions never depend on a module revision, so a
missing lineage raises nothing and `alembic upgrade heads` reports success
against a database that never got the module's schema.

## Why appending to the ScriptDirectory, and not `version_locations`

Alembic reads `version_locations` in `ScriptDirectory.from_config`, which
`command.upgrade` calls BEFORE `script.run_env()`. A
`config.set_main_option("version_locations", ...)` inside `alembic/env.py`
therefore mutates a config nothing consults again. The `ScriptDirectory` object
is still mutable — its revision map is lazy and is not materialized until the
migrations run — so appending to `script.version_locations` is read by the
object that actually walks the graph.

## Why this list has one owner

Four call sites build a revision map and each answers a different question, so
each must choose deliberately whether a composed lineage belongs in its answer:

- `alembic/env.py` — composes. This is the migration itself.
- `scripts/ci/migrated_test_database.py` — composes. Its expected heads must
  equal what `command.upgrade` actually applies, or the test-database contract
  refuses every database the real chain produced.
- `scripts/setup/deploy_reconcile.py` — composes. It compares deployed heads
  against expected ones; a blind expectation reports permanent drift.
- `scripts/new_migration.py` — deliberately does NOT compose. It allocates a
  revision in SUB's lineage and needs Sub's single own head; a composed map is
  multi-headed and it would refuse to allocate at all.

Keeping the list here rather than in `env.py` means those four cannot drift.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from alembic.script import ScriptDirectory

#: Import names of every composed module distribution. A module is composed when
#: it is pinned in `pyproject.toml`, listed here, and has its prerequisites bound
#: in `app/migration_bindings.py`; `tests/architecture/test_composed_module_lineages.py`
#: fails when those three disagree.
COMPOSED_MODULE_LINEAGES: Final[tuple[str, ...]] = ("dotmac_service_orders",)


def composed_version_locations() -> tuple[str, ...]:
    """Return each composed module's shipped revisions directory.

    A module absent from the environment raises `ModuleNotFoundError` rather
    than being skipped: the pin says it is installed, and continuing without its
    lineage is the silent failure this module exists to prevent.
    """
    return tuple(
        str(import_module(f"{import_name}.migrations").versions_dir())
        for import_name in COMPOSED_MODULE_LINEAGES
    )


def compose_module_lineages(script: ScriptDirectory) -> None:
    """Append every composed module's lineage to a live script directory.

    Must be called before the revision map is materialized — that is, before any
    `get_heads()`, `walk_revisions()` or migration run.
    """
    for location in composed_version_locations():
        if location not in script.version_locations:
            script.version_locations.append(location)
