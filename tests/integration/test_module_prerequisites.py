"""Sub supplies four shared effects from its own migration lineage.

ADR-0011 rules that Sub composes module lineages beside its own chain and
answers their declared prerequisites with its OWN revisions, rather than by
running kernel `0001` — the position the kernel documents for ERP, which hosts
`public.tenants` in its own lineage and structurally cannot run `0001` either.

This file is the proof, and it is deliberately not a hand-written re-statement
of the contract. It calls the kernel's own verifier against the real migrated
database, so what is asserted here is exactly what a module migration will
assert at `alembic upgrade` — not a second, drifting copy of the same rules.
The ADR's phrase for this is "binding is not belief".

Four effects:

- ``tenant_scope_catalog.v1`` — `public.tenants` and `public.tenant_domains`
  with the kernel column/key/index contract, plus `app_current_tenant_id()`
  reading the `app.current_tenant` GUC as uuid and returning NULL when unset or
  malformed. Supplied by 508/509 (the tables) and 545 (the function and the four
  timestamp defaults 508 never set).
- ``module_database_roles.v1`` — `app_admin` (BYPASSRLS, not superuser),
  `app_user` and `platform_api` (neither). Supplied by 546.
- ``idempotency_ledger.v1`` — kernel-shaped tenant and platform at-most-once
  ledgers, with the exact key/index/plane contract. Supplied by 554. This is a
  persistence prerequisite for composed modules, not a runtime owner cutover:
  Sub's ``IdempotencyKey``, ``TaskExecution`` and ``idempotent_task`` paths
  remain authoritative and no row is copied into the new tables.
- ``outbox_relay.v1`` — tenant and platform module-event ledgers plus hardened
  claim/settle functions. Supplied by 555 after the explicit dispatcher-role
  bootstrap; every incumbent Sub outbox and dispatcher stays authoritative.

PostgreSQL only. Roles, functions and column defaults are exactly the things the
SQLite lane cannot represent, which is why this is not a unit test.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from dotmac_kernel.migrations.verify import (
    PrerequisiteNotSatisfiedError,
    require_prerequisites,
)
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
    install_prerequisite_bindings,
)
from sqlalchemy.engine import Engine

from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)

REQUIRED = (
    TENANT_SCOPE_CATALOG_V1.name,
    MODULE_DATABASE_ROLES_V1.name,
    IDEMPOTENCY_LEDGER_V1.name,
    OUTBOX_RELAY_V1.name,
)

OPERATOR_TENANT_ID = "8c7ae830-51fc-52ae-9818-d84b2a35e568"
SECOND_TENANT_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def rollback_connection(engine: Engine):
    """A connection whose DDL is discarded, so a test may break the schema."""
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def test_sub_supplies_every_bound_effect_from_its_own_lineage(
    engine: Engine,
) -> None:
    """The load-bearing assertion, in the exact form a module migration makes."""
    with engine.connect() as connection:
        require_prerequisites(connection, REQUIRED)


@pytest.mark.parametrize("name", REQUIRED)
def test_each_effect_is_verifiable_rather_than_merely_declared(
    engine: Engine, name: str
) -> None:
    """A prerequisite with no verifier raises rather than passing quietly.

    Worth asserting per-effect: `require_prerequisites` refuses a registered but
    unverifiable name, so this also proves none of ours is in that state.
    """
    with engine.connect() as connection:
        require_prerequisites(connection, (name,))


def test_the_tenant_function_answers_the_guc(engine: Engine) -> None:
    """Behaviour, not just definition: the three cases RLS depends on.

    The verifier reads the function's TEXT for its semantic markers. That
    catches a rewrite, but it cannot catch a Postgres-version difference in how
    the body behaves, and every composed module's RLS policy is this function's
    return value.
    """
    with engine.connect() as connection:
        unset = connection.execute(sa.text("SELECT app_current_tenant_id()")).scalar()
        assert unset is None, "unset GUC must resolve to NULL, not raise"

        connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :value, true)"),
            {"value": "not-a-uuid"},
        )
        malformed = connection.execute(
            sa.text("SELECT app_current_tenant_id()")
        ).scalar()
        assert malformed is None, (
            "a malformed GUC must resolve to NULL — raising here would make "
            "every RLS policy in every composed module error instead of "
            "denying"
        )

        connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :value, true)"),
            {"value": "11111111-1111-1111-1111-111111111111"},
        )
        resolved = connection.execute(
            sa.text("SELECT app_current_tenant_id()")
        ).scalar()
        assert str(resolved) == "11111111-1111-1111-1111-111111111111"


def test_the_verifier_bites_when_the_function_is_missing(rollback_connection) -> None:
    """Sensitivity proof.

    Without this, `test_sub_supplies_every_effect…` could pass because the
    verifier is inert rather than because Sub satisfies the contract. Dropping
    the function inside a rolled-back transaction is the cheapest way to prove
    it actually inspects this database.

    CASCADE because RLS policies depend on the function, and the number of them
    grows: `551_machine_credentials` added one and the plain DROP started
    failing with `DependentObjectsStillExist`. The whole statement runs inside a
    transaction this fixture rolls back, so cascading is contained — and it
    makes the proof stronger rather than weaker, since the function is then
    genuinely absent rather than absent-except-for-its-dependents.
    """
    rollback_connection.execute(
        sa.text("DROP FUNCTION app_current_tenant_id() CASCADE;")
    )

    with pytest.raises(PrerequisiteNotSatisfiedError):
        require_prerequisites(rollback_connection, (TENANT_SCOPE_CATALOG_V1.name,))


def test_the_verifier_bites_on_a_wrong_role_posture(rollback_connection) -> None:
    """The other half, and the one that matters most.

    A superuser bypasses RLS whether or not `rolbypassrls` is set, so an online
    role that acquired SUPERUSER would defeat tenant isolation for every
    composed module while still looking correct to a naive check. Proving the
    verifier catches it is proving the isolation claim has something behind it.
    """
    rollback_connection.execute(sa.text("ALTER ROLE app_user BYPASSRLS;"))

    with pytest.raises(PrerequisiteNotSatisfiedError):
        require_prerequisites(rollback_connection, (MODULE_DATABASE_ROLES_V1.name,))


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        pytest.param(
            "DROP TABLE public.idempotency_records",
            "does not exist",
            id="tenant-ledger-absent",
        ),
        pytest.param(
            "DROP TABLE public.platform_idempotency_records",
            "does not exist",
            id="platform-ledger-absent",
        ),
        pytest.param(
            "ALTER TABLE public.idempotency_records DROP CONSTRAINT "
            "uq_idempotency_records_tenant_scope_key",
            "no unique constraint",
            id="tenant-key-widened",
        ),
        pytest.param(
            "ALTER TABLE public.platform_idempotency_records DROP CONSTRAINT "
            "uq_platform_idempotency_records_scope_key",
            "no unique constraint",
            id="platform-key-widened",
        ),
        pytest.param(
            "ALTER TABLE public.idempotency_records DROP COLUMN fingerprint",
            "columns differ",
            id="tenant-fingerprint-overloaded-away",
        ),
        pytest.param(
            "ALTER TABLE public.platform_idempotency_records DROP COLUMN fingerprint",
            "columns differ",
            id="platform-fingerprint-overloaded-away",
        ),
        pytest.param(
            "ALTER TABLE public.idempotency_records NO FORCE ROW LEVEL SECURITY",
            "FORCEd row-level security",
            id="tenant-ledger-unforced",
        ),
        pytest.param(
            "ALTER TABLE public.platform_idempotency_records ENABLE ROW LEVEL SECURITY",
            "must carry no",
            id="platform-ledger-policied",
        ),
        pytest.param(
            "DROP INDEX public.ix_idempotency_records_expires_at",
            "no index on",
            id="tenant-retention-unindexed",
        ),
        pytest.param(
            "DROP INDEX public.ix_platform_idempotency_records_expires_at",
            "no index on",
            id="platform-retention-unindexed",
        ),
    ],
)
def test_the_idempotency_verifier_refuses_each_broken_observable(
    rollback_connection, statement: str, expected: str
) -> None:
    """A familiar table name cannot satisfy a structurally broken contract."""
    rollback_connection.execute(sa.text(statement))

    with pytest.raises(PrerequisiteNotSatisfiedError, match=expected):
        require_prerequisites(rollback_connection, (IDEMPOTENCY_LEDGER_V1.name,))


def test_idempotency_plane_catalog_and_privileges_are_exact(engine: Engine) -> None:
    """Prove the security contract the kernel's shape verifier cannot see."""
    inspector = sa.inspect(engine)
    tenant_columns = {
        column["name"]
        for column in inspector.get_columns("idempotency_records", schema="public")
    }
    platform_columns = {
        column["name"]
        for column in inspector.get_columns(
            "platform_idempotency_records", schema="public"
        )
    }
    assert "tenant_id" in tenant_columns
    assert "tenant_id" not in platform_columns

    with engine.connect() as connection:
        tenant_posture = connection.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'idempotency_records'"
            )
        ).one()
        platform_posture = connection.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' "
                "AND c.relname = 'platform_idempotency_records'"
            )
        ).one()
        assert tuple(bool(value) for value in tenant_posture) == (True, True)
        assert tuple(bool(value) for value in platform_posture) == (False, False)

        policy = connection.execute(
            sa.text(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE schemaname = 'public' "
                "AND tablename = 'idempotency_records' "
                "AND policyname = 'idempotency_records_tenant_isolation'"
            )
        ).one()
        assert "tenant_id = app_current_tenant_id()" in str(policy.qual)
        assert "tenant_id = app_current_tenant_id()" in str(policy.with_check)

        for role in ("platform_api", "app_admin"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert connection.scalar(
                    sa.text(
                        "SELECT has_table_privilege("
                        ":role, 'public.platform_idempotency_records', :privilege)"
                    ),
                    {"role": role, "privilege": privilege},
                )

        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ):
            assert not connection.scalar(
                sa.text(
                    "SELECT has_table_privilege("
                    "'app_user', 'public.platform_idempotency_records', "
                    ":privilege)"
                ),
                {"privilege": privilege},
            )
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            assert not connection.scalar(
                sa.text(
                    "SELECT has_any_column_privilege("
                    "'app_user', 'public.platform_idempotency_records', "
                    ":privilege)"
                ),
                {"privilege": privilege},
            )


def test_tenant_idempotency_rows_are_isolated_by_the_canonical_guc(
    rollback_connection,
) -> None:
    """Execute the policy as ``app_user`` for two real tenant identities."""
    rollback_connection.execute(
        sa.text(
            "INSERT INTO public.tenants (id, slug, name, is_active) "
            "VALUES (:id, 'idempotency-canary', 'Idempotency canary', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": SECOND_TENANT_ID},
    )
    rollback_connection.execute(
        sa.text(
            "INSERT INTO public.idempotency_records "
            "(id, tenant_id, scope, key, operation, status) VALUES "
            "(:first_id, :first_tenant, 'test.scope', 'first', 'test', 'executed'), "
            "(:second_id, :second_tenant, 'test.scope', 'second', 'test', 'executed')"
        ),
        {
            "first_id": "11111111-1111-4111-8111-111111111111",
            "first_tenant": OPERATOR_TENANT_ID,
            "second_id": "22222222-2222-4222-8222-222222222223",
            "second_tenant": SECOND_TENANT_ID,
        },
    )

    rollback_connection.execute(sa.text("SET LOCAL ROLE app_user"))
    for selected_tenant, expected in (
        (OPERATOR_TENANT_ID, {OPERATOR_TENANT_ID}),
        (SECOND_TENANT_ID, {SECOND_TENANT_ID}),
    ):
        rollback_connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": selected_tenant},
        )
        visible = set(
            rollback_connection.execute(
                sa.text(
                    "SELECT tenant_id::text FROM public.idempotency_records "
                    "WHERE scope = 'test.scope'"
                )
            ).scalars()
        )
        assert visible == expected

    rollback_connection.execute(
        sa.text("SELECT set_config('app.current_tenant', '', true)")
    )
    assert (
        rollback_connection.scalar(
            sa.text(
                "SELECT count(*) FROM public.idempotency_records "
                "WHERE scope = 'test.scope'"
            )
        )
        == 0
    )


def test_the_timestamp_defaults_exist_on_both_catalogue_tables(
    engine: Engine,
) -> None:
    """508 created these columns NOT NULL with no DEFAULT; 545 added the default.

    Called out separately because "the table exists" was mistaken for "the
    prerequisite is satisfied" — the column contract is the half that was
    actually missing, and a writer that is not Sub's ORM would have hit a NOT
    NULL violation.
    """
    inspector = sa.inspect(engine)
    for table in ("tenants", "tenant_domains"):
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        for column in ("created_at", "updated_at"):
            assert columns[column]["default"] is not None, (
                f"{table}.{column} has no server default; "
                "tenant_scope_catalog.v1 requires one"
            )
