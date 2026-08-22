"""Can a module's `mod_*` lineage actually compose into Sub's alembic?

ADR-0011's shadow phase, executed rather than described. The decision says Sub
composes an installable module's lineage BESIDE its own chain — same version
table, one more `version_locations` entry, running under Sub's own `env.py`.
Everything about that was reasoned from reading files. This runs it.

## Why a stand-in lineage rather than `dotmac-ipam`

ADR-0038 deliberately withholds the nine network modules from
`.github/release-modules.json`: publishing one before the Sub-first cutover would
create an installable parallel owner beside the qualifying source. So there is
no `dotmac-ipam` to pin, and there will not be until the cutover this rehearsal
exists to de-risk.

**This is a rehearsal of the MECHANISM, not of the module.** It is shaped to
match `ip_0001_ipam` exactly where the shape is what carries risk: an independent
root (`down_revision = None`) with its own branch label, fully schema-qualified
DDL into one `mod_*` schema, `require_prerequisites` before any DDL, and a table
whose bare name already exists in Sub's `public` schema. Substituting the real
package at cutover changes the DDL, not any of that. What it does NOT prove is
anything about IPAM's own tables or semantics.

## What is new here

Sub's guard fix (#2568) proved schema-qualified DDL survives the idempotent
wrappers at the `op` level. The prerequisite tests (#2567) proved both effects
verify. Neither ran a foreign lineage through Sub's real `env.py` against a
production-shaped schema, which is where three untested claims live:

1. Sub's `alembic_version` — widened to `VARCHAR(255)` and pre-created by
   `ensure_alembic_version_table` — tolerates a second, independent head.
2. Sub's chain stays single-headed and its head row is untouched.
3. The idempotent wrappers, installed for every revision in the run, do not
   skip the module's qualified DDL when a bare name collides. The
   `addresses` collision is real: `dotmac-ipam` owns an IP address,
   `app/models/subscriber.py` owns a street address.

Note the contrast with `test_kernel_lineage_rehearsal.py`, which deliberately
runs the KERNEL lineage through a distinct version table and its own environment
because that lineage conflicts with Sub's. A module lineage is the opposite
case: composing into Sub's own version table under Sub's own `env.py` IS the
thing being tested, so anything that isolated it would test nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from alembic import command
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

# Reused rather than re-copied: provisioning a disposable database and pointing
# Sub's settings at it is solved, and a second copy would drift from it. Bound
# by assignment rather than `from ... import` so the name does not read as a
# redefinition where the fixtures below take it as a parameter.
_render = kernel_rehearsal._render
isolated_database = kernel_rehearsal.isolated_database

SCHEMA = "mod_rehearsal"
BRANCH = "module_rehearsal"
REVISION = "rh_0001_module_rehearsal"

#: The bare name that already exists in Sub's `public` schema. This is the whole
#: point of the fixture: under the pre-#2568 guards, creating it here silently
#: no-opped and the revision was stamped as applied with the table absent.
COLLIDING_TABLE = "addresses"

_STANDIN_MIGRATION = f'''\
"""Stand-in module lineage for the ADR-0011 composition rehearsal.

Shaped like `ip_0001_ipam`: independent root, own branch label, fully
schema-qualified DDL, prerequisites verified before any DDL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from dotmac_kernel.migrations.verify import require_prerequisites
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from alembic import op

revision: str = "{REVISION}"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("{BRANCH}",)
depends_on: str | Sequence[str] | None = None

_SCHEMA = "{SCHEMA}"


def upgrade() -> None:
    # Before any DDL, exactly as a real module migration does it.
    require_prerequisites(
        op.get_bind(),
        (TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
    )

    op.execute(f"CREATE SCHEMA IF NOT EXISTS {{_SCHEMA}};")
    op.execute(f"GRANT USAGE ON SCHEMA {{_SCHEMA}} TO app_user, platform_api;")

    op.create_table(
        "{COLLIDING_TABLE}",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_rehearsal_addresses_tenant",
        "{COLLIDING_TABLE}",
        ["tenant_id"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rehearsal_addresses_tenant", "{COLLIDING_TABLE}", schema=_SCHEMA
    )
    op.drop_table("{COLLIDING_TABLE}", schema=_SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {{_SCHEMA}} CASCADE;")
'''


@pytest.fixture
def standin_lineage(tmp_path: Path) -> Path:
    """A module lineage on disk, in the layout `version_locations` expects."""
    versions = tmp_path / "standin_module" / "versions"
    versions.mkdir(parents=True)
    (versions / f"{REVISION}.py").write_text(_STANDIN_MIGRATION, encoding="utf-8")
    return versions


def _composed_config(database_url: URL, standin: Path) -> Config:
    """Sub's own alembic, with one more lineage composed in.

    This is the whole composition contract: `script_location` stays Sub's, so
    Sub's `env.py` runs — including `ensure_alembic_version_table` and the
    idempotent schema-op wrappers — and the module's directory joins
    `version_locations`.
    """
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option(
        "version_locations", f"{Path('alembic/versions').resolve()} {standin}"
    )
    config.set_main_option("path_separator", "space")
    config.set_main_option("sqlalchemy.url", _render(database_url))
    return config


def _heads(engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT version_num FROM alembic_version"))
        return {row[0] for row in rows}


def _columns(engine, table: str, schema: str) -> set[str]:
    inspector = sa.inspect(engine)
    if table not in inspector.get_table_names(schema=schema):
        return set()
    return {c["name"] for c in inspector.get_columns(table, schema=schema)}


@dataclass(frozen=True)
class _Composed:
    """What the rehearsal measured before composing, and the live engine."""

    engine: object
    sub_heads: set[str]
    public_columns: set[str]


@pytest.fixture
def composed(isolated_database: URL, standin_lineage: Path):
    """Sub's chain to head, then the module lineage composed on top.

    Yields the engine plus what was true BEFORE composition, so the assertions
    compare against a measured baseline rather than a remembered one.
    """
    sub_only = Config("alembic.ini")
    sub_only.set_main_option("script_location", "alembic")
    sub_only.set_main_option("sqlalchemy.url", _render(isolated_database))
    command.upgrade(sub_only, "heads")

    engine = create_engine(isolated_database)
    sub_heads = _heads(engine)
    public_columns = _columns(engine, COLLIDING_TABLE, "public")

    # The fixture only means something if the collision is real. If Sub ever
    # renames or drops this table, the rehearsal must say so rather than pass
    # while proving nothing.
    assert public_columns, (
        f"public.{COLLIDING_TABLE} does not exist, so this rehearsal no longer "
        "exercises a bare-name collision — pick a name from the six real ones "
        "or fix the fixture"
    )

    command.upgrade(_composed_config(isolated_database, standin_lineage), "heads")
    try:
        yield _Composed(engine, sub_heads, public_columns)
    finally:
        engine.dispose()


def test_the_module_lineage_composes_beside_subs_own(composed) -> None:
    """THE rehearsal.

    Every read-only claim in one test on purpose: each `composed` costs a full
    run of Sub's migration chain in a fresh database, so splitting these into a
    test apiece would buy nothing but minutes. The mutating cases below get
    their own composition because they have to.
    """
    inspector = sa.inspect(composed.engine)

    # 1. The module's schema and table exist. Under the pre-#2568 guards the
    #    table was silently absent while the revision was stamped as applied.
    assert SCHEMA in inspector.get_schema_names()
    assert COLLIDING_TABLE in inspector.get_table_names(schema=SCHEMA), (
        f"{SCHEMA}.{COLLIDING_TABLE} was not created. The guard answered about "
        f"public.{COLLIDING_TABLE} and the revision was stamped as applied with "
        "the table absent — the silent-corruption path ADR-0011 names"
    )

    # 2. It is the module's table, not a view of Sub's.
    assert {
        c["name"] for c in inspector.get_columns(COLLIDING_TABLE, schema=SCHEMA)
    } == {"id", "tenant_id", "address"}

    # 3. The other direction: qualified DDL did not reach into `public`.
    #    Compared against columns measured BEFORE composing, so an ALTER is
    #    caught as well as a create.
    after = _columns(composed.engine, COLLIDING_TABLE, "public")
    assert after == composed.public_columns, (
        f"public.{COLLIDING_TABLE} changed when the module lineage ran: "
        f"added {sorted(after - composed.public_columns)}, "
        f"removed {sorted(composed.public_columns - after)}"
    )

    # 4. Both lineages hold their own head. The ledger's stated fear about
    #    composition was "two independent heads in one version table"; for a
    #    module lineage that is not a hazard, it is the design — and the
    #    widened VARCHAR(255) accommodates a descriptive module revision id.
    heads = _heads(composed.engine)
    assert REVISION in heads, "the module head was not recorded"
    assert heads == composed.sub_heads | {REVISION}, (
        f"Sub's own heads changed when the module lineage composed: "
        f"was {sorted(composed.sub_heads)}, now {sorted(heads)}"
    )


def test_composing_again_is_a_no_op(composed, isolated_database, standin_lineage):
    """Deploys re-run `upgrade heads`; a second run must change nothing."""
    before = _heads(composed.engine)

    command.upgrade(_composed_config(isolated_database, standin_lineage), "heads")

    assert _heads(composed.engine) == before
    assert COLLIDING_TABLE in sa.inspect(composed.engine).get_table_names(schema=SCHEMA)


def test_the_module_lineage_downgrades_cleanly(
    composed, isolated_database, standin_lineage
) -> None:
    """Removing a composed module is the rollback half of the cutover plan.

    ADR-0011 says the module's own `downgrade()` owns this. Proving it here
    means "remove the module" is a rehearsed operation rather than an assumption
    written in a rollback section.
    """
    command.downgrade(
        _composed_config(isolated_database, standin_lineage), f"{BRANCH}@base"
    )

    assert SCHEMA not in sa.inspect(composed.engine).get_schema_names()
    assert _heads(composed.engine) == composed.sub_heads, (
        "Sub's own head did not survive the module's downgrade"
    )
