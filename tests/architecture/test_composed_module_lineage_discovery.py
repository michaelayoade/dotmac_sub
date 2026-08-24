"""Does Sub's REAL migration entry path discover a composed module's lineage?

`tests/architecture/test_composed_module_lineages.py` already checks that a
composed module is pinned, exposes a `versions_dir()`, and has its prerequisites
bound. Every one of those reads source or metadata. None of them runs Alembic,
so all of them pass while the lineage is invisible to the migration that matters.

`tests/integration/test_module_lineage_rehearsal.py` and
`tests/integration/test_kernel_lineage_rehearsal.py` DO run Alembic. Both build
their own `Config` and call `set_main_option("version_locations", ...)` BEFORE
`command.upgrade`. That is the working order — which is exactly why neither says
anything about the production path. They prove the MECHANISM composes; they
cannot catch an assembly that wires the mechanism up too late, because they
never use the assembly's wiring.

Alembic reads `version_locations` in `ScriptDirectory.from_config`, which
`command.upgrade` calls before `script.run_env()`. A `set_main_option` inside
`env.py` therefore mutates a config nothing will consult again. The
`ScriptDirectory` object itself is still mutable — its revision map is lazy and
is not materialized until the migrations run — so appending to
`context.script.version_locations` inside `env.py` DOES take effect.

The failure this guards is silent by construction: Sub's own revisions never
depend on a module revision, so a missing module lineage raises nothing.
`alembic upgrade heads` reports success, the module's schema was never created,
and the first query against it is the error message.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory

from app.migration_lineages import COMPOSED_MODULE_LINEAGES

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _declared_lineages() -> tuple[str, ...]:
    """The composed lineages, from their one owner in `app/migration_lineages.py`."""
    return COMPOSED_MODULE_LINEAGES


def _sub_config() -> Config:
    """Sub's REAL entry path: the checked-in ini, nothing set late.

    Deliberately identical to `_sub_config` in the kernel rehearsal, and
    deliberately NOT setting `version_locations` — setting it here would
    reproduce the bug's workaround and test the workaround instead of the code.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def _revisions_visible_through_env_py() -> tuple[set[str], tuple[str, ...]]:
    """Return the revision ids and version locations the real path ends up with.

    Runs `env.py` exactly as Alembic runs it — through `EnvironmentContext` on a
    `ScriptDirectory` built by `from_config` — but in offline mode with a no-op
    migration function, so it needs no database and applies nothing.
    """
    config = _sub_config()
    script = ScriptDirectory.from_config(config)

    with EnvironmentContext(
        config,
        script,
        fn=lambda revision, context: [],
        as_sql=True,
        starting_rev="base",
        destination_rev="heads",
    ):
        script.run_env()

    return (
        {revision.revision for revision in script.walk_revisions()},
        tuple(script.version_locations),
    )


@pytest.mark.parametrize("import_name", _declared_lineages())
def test_a_composed_lineage_is_visible_to_the_real_migration_path(
    import_name: str,
) -> None:
    """The module's revisions must be in the map Alembic actually walks."""
    versions_dir = import_module(f"{import_name}.migrations").versions_dir()
    expected = {
        path.stem for path in versions_dir.glob("*.py") if not path.stem.startswith("__")
    }
    assert expected, f"{import_name}: the installed distribution ships no revisions"

    visible, locations = _revisions_visible_through_env_py()
    missing = sorted(expected - visible)

    assert not missing, (
        f"{import_name}: `alembic upgrade heads` would silently skip these "
        f"revisions — they are not in the revision map Alembic walks.\n"
        f"  missing: {missing}\n"
        f"  version_locations in effect: {locations}\n"
        "`env.py` must append to `context.script.version_locations`; a late "
        "`config.set_main_option('version_locations', ...)` is read by nobody."
    )
