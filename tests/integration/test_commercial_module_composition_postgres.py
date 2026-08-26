"""Billing and Collections tenant schemas compose without moving authority."""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from dotmac_billing.models import (
    PLATFORM_TABLES as BILLING_PLATFORM_TABLES,
)
from dotmac_billing.models import (
    TENANT_TABLES as BILLING_TENANT_TABLES,
)
from dotmac_collections.models import (
    PLATFORM_TABLES as COLLECTIONS_PLATFORM_TABLES,
)
from dotmac_collections.models import (
    TENANT_TABLES as COLLECTIONS_TENANT_TABLES,
)
from sqlalchemy.engine import URL

from alembic import command
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database

PROVIDER_HEAD = "557_outbox_relay_prereq"
BILLING_HEAD = "bi_0001_billing"
COLLECTIONS_HEAD = "cl_0001_collections"
SECOND_TENANT = "44444444-4444-4444-8444-444444444444"


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option(
        "sqlalchemy.url", database_url.render_as_string(hide_password=False)
    )
    return config


def _heads(connection: sa.Connection) -> set[str]:
    return set(
        connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    )


def _assert_tenant_catalog(
    engine: sa.Engine,
    *,
    schema: str,
    tenant_tables: tuple[str, ...],
    platform_tables: tuple[str, ...],
) -> None:
    inspector = sa.inspect(engine)
    actual = set(inspector.get_table_names(schema=schema))
    assert actual == set(tenant_tables)
    assert not actual & set(platform_tables)
    with engine.connect() as connection:
        posture = {
            row.table_name: (row.rls_enabled, row.rls_forced)
            for row in connection.execute(
                sa.text(
                    """
                    SELECT c.relname AS table_name,
                           c.relrowsecurity AS rls_enabled,
                           c.relforcerowsecurity AS rls_forced
                      FROM pg_class AS c
                      JOIN pg_namespace AS n ON n.oid = c.relnamespace
                     WHERE n.nspname = :schema
                       AND c.relname = ANY(:tables)
                    """
                ),
                {"schema": schema, "tables": list(tenant_tables)},
            )
        }
    assert posture == dict.fromkeys(tenant_tables, (True, True))


def _seed_canaries(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO public.tenants (id, slug, name, is_active) "
            "VALUES (:id, 'commercial-canary', 'Commercial canary', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": SECOND_TENANT},
    )
    for tenant_id, suffix in (
        (str(OPERATOR_TENANT_ID), "operator"),
        (SECOND_TENANT, "second"),
    ):
        connection.execute(
            sa.text(
                "INSERT INTO mod_billing.billing_accounts "
                "(id, tenant_id, external_account_ref, currency, minor_units) "
                "VALUES (:id, :tenant, :reference, 'NGN', 2)"
            ),
            {
                "id": uuid4(),
                "tenant": tenant_id,
                "reference": f"billing-{suffix}",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO mod_coll.collection_policies "
                "(id, tenant_id, policy_code, description) "
                "VALUES (:id, :tenant, :code, :description)"
            ),
            {
                "id": uuid4(),
                "tenant": tenant_id,
                "code": f"collections-{suffix}",
                "description": f"{suffix} canary",
            },
        )


def _visible_codes(
    database_url: URL, tenant_id: str | None
) -> tuple[set[str], set[str]]:
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE app_user"))
            if tenant_id is not None:
                connection.execute(
                    sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
                    {"tenant": tenant_id},
                )
            billing = set(
                connection.execute(
                    sa.text(
                        "SELECT external_account_ref "
                        "FROM mod_billing.billing_accounts "
                        "WHERE external_account_ref LIKE 'billing-%'"
                    )
                ).scalars()
            )
            collections = set(
                connection.execute(
                    sa.text(
                        "SELECT policy_code FROM mod_coll.collection_policies "
                        "WHERE policy_code LIKE 'collections-%'"
                    )
                ).scalars()
            )
            return billing, collections
    finally:
        engine.dispose()


def test_fresh_upgrade_selects_only_both_tenant_planes_with_effective_rls(
    isolated_database: URL,
) -> None:
    config = _config(isolated_database)
    command.upgrade(config, "heads")
    engine = sa.create_engine(isolated_database)
    try:
        _assert_tenant_catalog(
            engine,
            schema="mod_billing",
            tenant_tables=BILLING_TENANT_TABLES,
            platform_tables=BILLING_PLATFORM_TABLES,
        )
        _assert_tenant_catalog(
            engine,
            schema="mod_coll",
            tenant_tables=COLLECTIONS_TENANT_TABLES,
            platform_tables=COLLECTIONS_PLATFORM_TABLES,
        )
        with engine.begin() as connection:
            heads = _heads(connection)
            assert {BILLING_HEAD, COLLECTIONS_HEAD} <= heads
            _seed_canaries(connection)

        assert _visible_codes(isolated_database, str(OPERATOR_TENANT_ID)) == (
            {"billing-operator"},
            {"collections-operator"},
        )
        assert _visible_codes(isolated_database, SECOND_TENANT) == (
            {"billing-second"},
            {"collections-second"},
        )
        assert _visible_codes(isolated_database, None) == (set(), set())

        with engine.connect() as connection:
            before = _heads(connection)
        command.upgrade(config, "heads")
        with engine.connect() as connection:
            assert _heads(connection) == before
    finally:
        engine.dispose()


def test_upgrade_from_relay_provider_preserves_module_event_storage(
    isolated_database: URL,
) -> None:
    config = _config(isolated_database)
    command.upgrade(config, PROVIDER_HEAD)
    engine = sa.create_engine(isolated_database)
    event_id = uuid4()
    try:
        with engine.begin() as connection:
            assert _heads(connection) == {PROVIDER_HEAD}
            connection.execute(
                sa.text(
                    "INSERT INTO public.outbox_events "
                    "(id, tenant_id, event_type, payload) "
                    "VALUES (:id, :tenant, 'commercial.predecessor', '{}')"
                ),
                {"id": event_id, "tenant": str(OPERATOR_TENANT_ID)},
            )
        command.upgrade(config, "heads")
        with engine.connect() as connection:
            assert {BILLING_HEAD, COLLECTIONS_HEAD} <= _heads(connection)
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM public.outbox_events WHERE id = :id"),
                    {"id": event_id},
                )
                == 1
            )
    finally:
        engine.dispose()
