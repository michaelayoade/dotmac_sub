"""Real-migration proofs for Sub's tenant-only Billing rehearsal."""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from dotmac_billing import module as billing_module
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


def _database_url() -> str:
    value = os.environ.get("SUB_BILLING_SHADOW_DATABASE_URL")
    if value is None:
        raise RuntimeError(
            "SUB_BILLING_SHADOW_DATABASE_URL must name an explicitly disposable "
            "migrated PostgreSQL database"
        )
    database = urlparse(value).path.lstrip("/").lower()
    if not any(marker in database for marker in ("test", "pytest", "ci", "e2e")):
        raise RuntimeError("the Billing shadow database name must identify test use")
    return value


@pytest.fixture(scope="module")
def database_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_real_graph_reaches_kernel_and_billing_heads(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        heads = set(
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        )

    assert heads == {"0026_platform_audit_log", "bi_0001_billing"}


@pytest.mark.postgres
def test_tenant_selection_installs_only_forced_rls_tables(
    database_engine: Engine,
) -> None:
    expected_tenant = set(billing_module.tables)
    expected_platform = set(billing_module.platform_tables)
    with database_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.relname AS table_name,
                       c.relrowsecurity AS rls_enabled,
                       c.relforcerowsecurity AS rls_forced
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'mod_billing' AND c.relkind = 'r'
                """
            )
        ).mappings()
        catalog = {
            str(row["table_name"]): (
                bool(row["rls_enabled"]),
                bool(row["rls_forced"]),
            )
            for row in rows
        }
        tenant_columns = {
            str(row["table_name"]): (
                str(row["data_type"]),
                str(row["is_nullable"]),
            )
            for row in connection.execute(
                text(
                    """
                    SELECT table_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'mod_billing'
                      AND column_name = 'tenant_id'
                    """
                )
            ).mappings()
        }
        policy_tables = set(
            connection.execute(
                text(
                    """
                    SELECT DISTINCT tablename
                    FROM pg_policies
                    WHERE schemaname = 'mod_billing'
                    """
                )
            ).scalars()
        )

    assert set(catalog) == expected_tenant
    assert not expected_platform & set(catalog)
    assert all(flags == (True, True) for flags in catalog.values())
    assert tenant_columns == {table: ("uuid", "NO") for table in expected_tenant}
    assert policy_tables == expected_tenant


@pytest.mark.postgres
def test_tenant_grants_are_exact_and_platform_role_has_no_schema_access(
    database_engine: Engine,
) -> None:
    expected_mutable = {"documents", "document_artifacts"}
    expected = {
        table: {"SELECT", "INSERT"}
        | ({"UPDATE"} if table in expected_mutable else set())
        for table in billing_module.tables
    }
    with database_engine.connect() as connection:
        actual: dict[str, set[str]] = {table: set() for table in billing_module.tables}
        for row in connection.execute(
            text(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema = 'mod_billing' AND grantee = 'app_user'
                """
            )
        ).mappings():
            actual[str(row["table_name"])].add(str(row["privilege_type"]))
        column_grants = {
            (
                str(row["table_name"]),
                str(row["column_name"]),
                str(row["grantee"]),
                str(row["privilege_type"]),
            )
            for row in connection.execute(
                text(
                    """
                    SELECT c.relname AS table_name,
                           a.attname AS column_name,
                           grantee.rolname AS grantee,
                           acl.privilege_type::text AS privilege_type
                    FROM pg_attribute AS a
                    JOIN pg_class AS c ON c.oid = a.attrelid
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN LATERAL aclexplode(a.attacl) AS acl
                    JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                    WHERE n.nspname = 'mod_billing'
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                      AND a.attacl IS NOT NULL
                    """
                )
            ).mappings()
        }
        access = connection.execute(
            text(
                """
                SELECT has_schema_privilege('app_user', 'mod_billing', 'USAGE'),
                       has_schema_privilege('platform_api', 'mod_billing', 'USAGE')
                """
            )
        ).one()

    assert actual == expected
    assert column_grants == {("billing_accounts", "id", "app_user", "UPDATE")}
    assert tuple(access) == (True, False)


@pytest.mark.postgres
def test_app_user_rls_separates_two_tenants_and_refuses_cross_tenant_insert(
    database_engine: Engine,
) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    account_a = uuid4()
    account_b = uuid4()
    with database_engine.connect() as connection:
        connection.execute(
            text(
                """
                INSERT INTO public.tenants (id, slug, name)
                VALUES (:tenant_a, :slug_a, 'Billing test A'),
                       (:tenant_b, :slug_b, 'Billing test B')
                """
            ),
            {
                "tenant_a": tenant_a,
                "tenant_b": tenant_b,
                "slug_a": f"billing-test-{tenant_a}",
                "slug_b": f"billing-test-{tenant_b}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mod_billing.billing_accounts
                    (id, tenant_id, external_account_ref, currency, minor_units)
                VALUES (:account_a, :tenant_a, 'account-a', 'NGN', 2),
                       (:account_b, :tenant_b, 'account-b', 'NGN', 2)
                """
            ),
            {
                "account_a": account_a,
                "account_b": account_b,
                "tenant_a": tenant_a,
                "tenant_b": tenant_b,
            },
        )
        connection.execute(text("SET LOCAL ROLE app_user"))
        connection.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(tenant_a)},
        )
        visible_a = tuple(
            connection.execute(
                text(
                    "SELECT external_account_ref "
                    "FROM mod_billing.billing_accounts ORDER BY 1"
                )
            ).scalars()
        )
        connection.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(tenant_b)},
        )
        visible_b = tuple(
            connection.execute(
                text(
                    "SELECT external_account_ref "
                    "FROM mod_billing.billing_accounts ORDER BY 1"
                )
            ).scalars()
        )
        connection.rollback()

    assert visible_a == ("account-a",)
    assert visible_b == ("account-b",)

    with pytest.raises(DBAPIError), database_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE app_user"))
        connection.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(tenant_b)},
        )
        connection.execute(
            text(
                """
                INSERT INTO mod_billing.billing_accounts
                    (id, tenant_id, external_account_ref, currency, minor_units)
                VALUES (:account, :tenant_a, 'wrong-tenant', 'NGN', 2)
                """
            ),
            {"account": uuid4(), "tenant_a": tenant_a},
        )


@pytest.mark.postgres
def test_platform_role_is_refused_by_the_tenant_only_selection(
    database_engine: Engine,
) -> None:
    with pytest.raises(DBAPIError), database_engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE platform_api"))
        connection.execute(text("SELECT count(*) FROM mod_billing.billing_accounts"))
