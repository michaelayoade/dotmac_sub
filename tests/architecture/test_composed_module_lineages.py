"""A composed module is declared in three places, and they must agree.

Composing a Starter module into Sub is not one edit. It is a dependency pin in
`pyproject.toml`, a lineage appended in `alembic/env.py`, and a prerequisite
binding in `app/migration_bindings.py`. Each is load-bearing and each fails
differently when it is the one that was forgotten:

- **pinned, lineage missing** — `alembic upgrade head` reports success while
  the module's schema was never created. The application then imports a module
  whose tables do not exist, and the first query is the error message.
- **lineage present, unpinned** — `env.py` raises `ModuleNotFoundError` at
  import, which is loud and harmless. This is the safe direction.
- **both present, prerequisite unbound** — the module's migration asks for an
  effect nothing supplies. `require_prerequisites` refuses before any DDL, so
  this too is loud.

Only the first is silent, so it is the one these checks exist for. The others
are asserted anyway, because a guard that covers one direction invites the
belief that the others were considered.
"""

from __future__ import annotations

import ast
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PY = PROJECT_ROOT / "alembic" / "env.py"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _declared_lineages() -> tuple[str, ...]:
    """`_COMPOSED_MODULE_LINEAGES` read from source, not by importing `env.py`.

    Importing it would need a database URL and would run the composition it is
    supposed to be checking.
    """

    tree = ast.parse(ENV_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            first = node.targets[0]
            target = first.id if isinstance(first, ast.Name) else None
        if target == "_COMPOSED_MODULE_LINEAGES" and node.value is not None:
            return tuple(
                element.value
                for element in ast.walk(node.value)
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    raise AssertionError("alembic/env.py declares no _COMPOSED_MODULE_LINEAGES")


def _pinned_distributions() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    pinned: set[str] = set()
    for requirement in project.get("dependencies", []):
        name = (
            requirement.split("==")[0]
            .split(">=")[0]
            .split("[")[0]
            .strip()
        )
        if name:
            pinned.add(name)
    return pinned


def _distribution_for(import_name: str) -> str:
    return import_name.replace("_", "-")


def test_every_composed_lineage_is_pinned() -> None:
    """The silent failure: a lineage nothing installs, or a pin nothing runs."""

    pinned = _pinned_distributions()
    unpinned = sorted(
        _distribution_for(name)
        for name in _declared_lineages()
        if _distribution_for(name) not in pinned
    )
    assert not unpinned, (
        "alembic/env.py composes these lineages and pyproject.toml pins no such "
        "distribution:\n  " + "\n  ".join(unpinned)
    )


def test_every_pinned_dotmac_module_composes_its_lineage() -> None:
    """The other direction, and the one that fails silently.

    A pinned module whose lineage is not composed leaves `alembic upgrade head`
    reporting success against a database that never got its schema. The kernel
    and UI are excluded deliberately: Sub composes kernel MODELS without
    adopting kernel MIGRATIONS, and `dotmac-ui` ships no lineage at all.
    """

    exempt = {"dotmac-kernel", "dotmac-ui", "dotmac-integration-client"}
    composed = {_distribution_for(name) for name in _declared_lineages()}
    missing = sorted(
        name
        for name in _pinned_distributions()
        if name.startswith("dotmac-") and name not in exempt and name not in composed
    )
    assert not missing, (
        "these modules are pinned and their lineages are not composed in "
        "alembic/env.py, so `alembic upgrade head` would report success without "
        "creating their schemas:\n  " + "\n  ".join(missing)
    )


def test_every_composed_module_exposes_its_versions_directory() -> None:
    """`env.py` calls `versions_dir()`; a module without one fails at migration."""

    for import_name in _declared_lineages():
        migrations = import_module(f"{import_name}.migrations")
        versions = migrations.versions_dir()
        assert versions.is_dir(), f"{import_name}: {versions} is not a directory"
        assert any(versions.glob("*.py")), f"{import_name}: no revisions in {versions}"


def test_every_composed_module_has_its_prerequisites_bound() -> None:
    """A module's declared `requires` must have an answer in this assembly.

    The binding is checked again at `alembic upgrade` against the live catalog.
    This is the earlier, cheaper failure: a missing entry is a typo caught by
    the unit suite rather than a refused migration on a real database.
    """

    from app.migration_bindings import MIGRATION_BINDINGS

    unbound: list[str] = []
    for import_name in _declared_lineages():
        manifest = import_module(f"{import_name}.manifest").module
        for requirement in getattr(manifest, "requires", ()):
            if requirement not in MIGRATION_BINDINGS:
                unbound.append(f"{import_name} requires {requirement!r}")
    assert not unbound, (
        "app/migration_bindings.py has no revision supplying these effects:\n  "
        + "\n  ".join(unbound)
    )


def test_a_bound_revision_exists_in_subs_own_lineage() -> None:
    """A binding naming a revision Sub does not have is worse than none.

    It reads as answered while the effect is supplied by nothing, and the
    failure surfaces at `alembic upgrade` on whichever database runs first.
    """

    from app.migration_bindings import MIGRATION_BINDINGS

    versions = PROJECT_ROOT / "alembic" / "versions"
    revisions = {
        path.stem for path in versions.glob("*.py") if not path.stem.startswith("__")
    }
    dangling = sorted(
        f"{effect} -> {revision}"
        for effect, revision in MIGRATION_BINDINGS.items()
        if revision not in revisions
    )
    assert not dangling, (
        "these bindings name a revision that is not in alembic/versions:\n  "
        + "\n  ".join(dangling)
    )


@pytest.mark.parametrize("import_name", _declared_lineages())
def test_a_composed_module_owns_a_distinct_schema(import_name: str) -> None:
    """Two modules in one schema would make ownership unprovable."""

    manifest = import_module(f"{import_name}.manifest").module
    schema = manifest.db_schema
    assert schema.startswith("mod_"), f"{import_name}: unexpected schema {schema!r}"
    others = {
        import_module(f"{other}.manifest").module.db_schema
        for other in _declared_lineages()
        if other != import_name
    }
    assert schema not in others, f"{import_name}: shares schema {schema!r}"
