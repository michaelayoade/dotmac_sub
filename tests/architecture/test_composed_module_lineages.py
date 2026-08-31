"""A composed module is declared in three places, and they must agree.

Composing a Starter module into Sub is not one edit. It is a dependency pin in
`pyproject.toml`, a package resource in `alembic.ini`, and a prerequisite
binding in `app/migration_bindings.py`. Each is load-bearing and each fails
differently when it is the one that was forgotten:

- **pinned, lineage missing** — `alembic upgrade heads` reports success while
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

import configparser
import tomllib
from importlib import import_module, util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _declared_lineages() -> tuple[str, ...]:
    """Read the locations Alembic uses before ``env.py`` is executed."""

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI)
    entries = parser["alembic"]["version_locations"].split()
    return tuple(
        entry.removesuffix(".migrations:versions")
        for entry in entries
        if entry.endswith(".migrations:versions")
    )


def _pinned_distributions() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    pinned: set[str] = set()
    for requirement in project.get("dependencies", []):
        name = requirement.split("==")[0].split(">=")[0].split("[")[0].strip()
        if name:
            pinned.add(name)
    return pinned


def _distribution_for(import_name: str) -> str:
    return import_name.replace("_", "-")


def _import_name_for(distribution: str) -> str:
    return distribution.replace("-", "_")


def _ships_a_lineage(distribution: str) -> bool:
    """Whether the INSTALLED distribution actually provides migrations.

    This is what turns "stateless adapter" from an assertion into a measured
    premise. A package with no ``<pkg>.migrations`` has no lineage to compose,
    so it cannot be the silent failure this module exists to catch - and if it
    ever grows one, the exemption stops applying by itself rather than
    outliving the reason it was written.
    """

    try:
        return (
            util.find_spec(f"{_import_name_for(distribution)}.migrations") is not None
        )
    except (ImportError, ValueError, AttributeError):
        return False


def test_every_composed_lineage_is_pinned() -> None:
    """The silent failure: a lineage nothing installs, or a pin nothing runs."""

    pinned = _pinned_distributions()
    unpinned = sorted(
        _distribution_for(name)
        for name in _declared_lineages()
        if _distribution_for(name) not in pinned
    )
    assert not unpinned, (
        "alembic.ini composes these lineages and pyproject.toml pins no such "
        "distribution:\n  " + "\n  ".join(unpinned)
    )


def test_every_pinned_dotmac_module_composes_its_lineage() -> None:
    """The other direction, and the one that fails silently.

    A pinned module whose lineage is not composed leaves `alembic upgrade heads`
    reporting success against a database that never got its schema. The kernel
    and UI are excluded deliberately: Sub composes kernel MODELS without
    adopting kernel MIGRATIONS, and `dotmac-ui` ships no lineage at all.
    """

    # Sub composes kernel MODELS without adopting kernel MIGRATIONS. The kernel
    # does ship a lineage, so this one cannot be derived - it is a deliberate
    # decision and has to be stated.
    declared_exempt = {"dotmac-kernel"}
    composed = {_distribution_for(name) for name in _declared_lineages()}
    missing = sorted(
        name
        for name in _pinned_distributions()
        if name.startswith("dotmac-")
        and name not in declared_exempt
        and name not in composed
        # Everything else earns its exemption by measurement: no lineage
        # shipped, nothing that `alembic upgrade heads` could silently skip.
        and _ships_a_lineage(name)
    )
    assert not missing, (
        "these modules are pinned and their lineages are not composed in "
        "alembic.ini, so `alembic upgrade heads` would report success without "
        "creating their schemas:\n  " + "\n  ".join(missing)
    )


def test_the_lineage_probe_is_load_bearing() -> None:
    """The derived exemption must not exempt everything.

    `_ships_a_lineage` returning False for every distribution - packages absent
    from the environment, a renamed submodule - would make the check above pass
    by exempting its entire input. Prove the probe still says yes to something
    that genuinely ships a lineage, and no to something that genuinely does
    not, so a green run means the guard looked rather than shrugged.
    """

    composed = [_distribution_for(name) for name in _declared_lineages()]
    assert composed, "no lineages composed; this suite would be vacuous"
    assert all(_ships_a_lineage(name) for name in composed), (
        "a composed lineage is not visible to the probe, so every stateless "
        "exemption above is unproven: "
        + ", ".join(name for name in composed if not _ships_a_lineage(name))
    )
    assert not _ships_a_lineage("dotmac-auth-oidc"), (
        "dotmac-auth-oidc now ships migrations; it is no longer a stateless "
        "protocol adapter and must compose its lineage in alembic.ini"
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

    from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

    bound = {binding.prerequisite for binding in ASSEMBLY_PREREQUISITE_BINDINGS}
    unbound: list[str] = []
    for import_name in _declared_lineages():
        manifest = import_module(f"{import_name}.manifest").module
        for requirement in getattr(manifest, "requires", ()):
            if requirement not in bound:
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

    from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

    versions = PROJECT_ROOT / "alembic" / "versions"
    revisions = {
        path.stem for path in versions.glob("*.py") if not path.stem.startswith("__")
    }
    dangling = sorted(
        f"{binding.prerequisite} -> {binding.provider_revision}"
        for binding in ASSEMBLY_PREREQUISITE_BINDINGS
        if binding.provider_revision not in revisions
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
