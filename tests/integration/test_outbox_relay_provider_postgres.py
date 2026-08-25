"""PostgreSQL proof for Sub's product-first ``outbox_relay.v1`` provider."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

PREREQUISITE = "outbox_relay.v1"
FIRST_TENANT = "8c7ae830-51fc-52ae-9818-d84b2a35e568"
SECOND_TENANT = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _install_sub_bindings() -> Iterator[None]:
    from dotmac_kernel.prerequisites import (
        install_prerequisite_bindings,
        installed_bindings,
    )

    previous = tuple(installed_bindings())
    install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
    try:
        yield
    finally:
        install_prerequisite_bindings(previous)


@contextlib.contextmanager
def _broken(engine: Engine, statement: str) -> Iterator[Connection]:
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text(statement))
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def rollback_connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection, connection.begin() as transaction:
        try:
            yield connection
        finally:
            transaction.rollback()


def _seed_second_tenant(connection: Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO public.tenants (id, slug, name, is_active) "
            "VALUES (:id, 'outbox-canary', 'Outbox canary', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": SECOND_TENANT},
    )


def test_migrated_sub_satisfies_the_kernel_relay_contract(engine: Engine) -> None:
    from dotmac_kernel.migrations.verify import require_prerequisites

    with engine.connect() as connection:
        require_prerequisites(connection, (PREREQUISITE,))


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        pytest.param(
            "DROP TABLE public.platform_outbox_events CASCADE",
            "does not exist",
            id="platform-plane-absent",
        ),
        pytest.param(
            "DROP INDEX public.ix_outbox_events_status_leased_at",
            "no index on",
            id="stale-lease-reclaim-unindexed",
        ),
        pytest.param(
            "ALTER TABLE public.outbox_events NO FORCE ROW LEVEL SECURITY",
            "FORCEd row-level security",
            id="tenant-plane-unforced",
        ),
        pytest.param(
            "ALTER POLICY outbox_events_tenant_isolation ON public.outbox_events "
            "USING (true)",
            "do not restrict rows",
            id="tenant-policy-always-passes",
        ),
        pytest.param(
            "ALTER TABLE public.platform_outbox_events ENABLE ROW LEVEL SECURITY",
            "must carry no row-level security",
            id="platform-plane-policied",
        ),
        pytest.param(
            "GRANT SELECT ON TABLE public.platform_outbox_events TO app_user",
            "reachable by",
            id="platform-plane-exposed",
        ),
        pytest.param(
            "GRANT SELECT (payload) ON TABLE public.outbox_events TO outbox_dispatcher",
            "holds table or column privilege",
            id="dispatcher-given-column-grant",
        ),
        pytest.param(
            "ALTER ROLE outbox_dispatcher BYPASSRLS",
            "rolbypassrls",
            id="dispatcher-can-bypass-rls",
        ),
        pytest.param(
            "DROP FUNCTION public.settle_outbox_event"
            "(uuid, text, text, timestamptz, integer, text)",
            "does not exist",
            id="settle-missing",
        ),
        pytest.param(
            "ALTER FUNCTION public.claim_outbox_batch(text, integer, integer) "
            "SECURITY INVOKER",
            "not SECURITY DEFINER",
            id="claim-is-invoker",
        ),
        pytest.param(
            "ALTER FUNCTION public.claim_outbox_batch(text, integer, integer) "
            "SET search_path = public",
            "empty search_path",
            id="claim-path-unpinned",
        ),
        pytest.param(
            "GRANT EXECUTE ON FUNCTION public.claim_outbox_batch"
            "(text, integer, integer) TO PUBLIC",
            "granted to PUBLIC",
            id="claim-is-public",
        ),
    ],
)
def test_each_broken_observable_is_refused_specifically(
    engine: Engine, statement: str, expected: str
) -> None:
    from dotmac_kernel.migrations.verify import (
        PrerequisiteNotSatisfiedError,
        require_prerequisites,
    )

    with _broken(engine, statement) as connection:
        with pytest.raises(PrerequisiteNotSatisfiedError, match=expected):
            require_prerequisites(connection, (PREREQUISITE,))


def test_catalog_keys_defaults_indexes_roles_and_positive_grants_are_exact(
    engine: Engine,
) -> None:
    inspector = sa.inspect(engine)
    tenant_pk = inspector.get_pk_constraint("outbox_events", schema="public")
    platform_pk = inspector.get_pk_constraint("platform_outbox_events", schema="public")
    assert tenant_pk["constrained_columns"] == ["id"]
    assert platform_pk["constrained_columns"] == ["id"]
    tenant_fks = inspector.get_foreign_keys("outbox_events", schema="public")
    assert any(
        fk["constrained_columns"] == ["tenant_id"]
        and fk["referred_schema"] == "public"
        and fk["referred_table"] == "tenants"
        and fk["referred_columns"] == ["id"]
        for fk in tenant_fks
    )
    tenant_indexes = {
        index["name"]
        for index in inspector.get_indexes("outbox_events", schema="public")
    }
    assert "ix_outbox_events_tenant_id" in tenant_indexes
    for table in ("outbox_events", "platform_outbox_events"):
        defaults = {
            column["name"]: str(column["default"])
            for column in inspector.get_columns(table, schema="public")
        }
        assert "pending" in defaults["status"]

    with engine.connect() as connection:
        observed_roles = {
            str(row.rolname): (
                bool(row.rolcanlogin),
                bool(row.rolbypassrls),
                bool(row.rolsuper),
            )
            for row in connection.execute(
                sa.text(
                    "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper "
                    "FROM pg_roles WHERE rolname IN "
                    "('outbox_dispatcher', 'platform_outbox_dispatcher')"
                )
            )
        }
        assert observed_roles == {
            "outbox_dispatcher": (True, False, False),
            "platform_outbox_dispatcher": (True, False, False),
        }

        assert connection.scalar(
            sa.text(
                "SELECT has_schema_privilege('outbox_dispatcher', 'public', 'USAGE')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_schema_privilege("
                "'platform_outbox_dispatcher', 'public', 'USAGE')"
            )
        )
        for role in ("app_user", "platform_api", "app_admin"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert connection.scalar(
                    sa.text(
                        "SELECT has_table_privilege("
                        ":role, 'public.outbox_events', :privilege)"
                    ),
                    {"role": role, "privilege": privilege},
                )
        for role in ("platform_api", "app_admin"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert connection.scalar(
                    sa.text(
                        "SELECT has_table_privilege("
                        ":role, 'public.platform_outbox_events', :privilege)"
                    ),
                    {"role": role, "privilege": privilege},
                )


def test_app_user_rls_is_effective_for_two_tenants(
    rollback_connection: Connection,
) -> None:
    _seed_second_tenant(rollback_connection)
    rollback_connection.execute(
        sa.text(
            "INSERT INTO public.outbox_events "
            "(id, tenant_id, event_type, payload) VALUES "
            "(:first_id, :first_tenant, 'canary.first', '{}'), "
            "(:second_id, :second_tenant, 'canary.second', '{}')"
        ),
        {
            "first_id": "41111111-1111-4111-8111-111111111111",
            "first_tenant": FIRST_TENANT,
            "second_id": "42222222-2222-4222-8222-222222222222",
            "second_tenant": SECOND_TENANT,
        },
    )
    rollback_connection.execute(sa.text("SET LOCAL ROLE app_user"))
    for selected, expected_type in (
        (FIRST_TENANT, "canary.first"),
        (SECOND_TENANT, "canary.second"),
    ):
        rollback_connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": selected},
        )
        assert rollback_connection.execute(
            sa.text(
                "SELECT event_type FROM public.outbox_events "
                "WHERE event_type LIKE 'canary.%'"
            )
        ).scalars().all() == [expected_type]

    rollback_connection.execute(
        sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": FIRST_TENANT},
    )
    savepoint = rollback_connection.begin_nested()
    try:
        with pytest.raises(DBAPIError):
            rollback_connection.execute(
                sa.text(
                    "INSERT INTO public.outbox_events "
                    "(id, tenant_id, event_type, payload) "
                    "VALUES (:id, :tenant, 'canary.wrong', '{}')"
                ),
                {
                    "id": "43333333-3333-4333-8333-333333333333",
                    "tenant": SECOND_TENANT,
                },
            )
    finally:
        savepoint.rollback()


def test_dispatchers_have_only_their_own_function_pair(engine: Engine) -> None:
    own_functions = {
        "outbox_dispatcher": {
            "public.claim_outbox_batch(text, integer, integer)",
            "public.settle_outbox_event(uuid, text, text, timestamptz, integer, text)",
        },
        "platform_outbox_dispatcher": {
            "public.claim_platform_outbox_batch(text, integer, integer)",
            "public.settle_platform_outbox_event(uuid, text, text, timestamptz, integer, text)",
        },
    }
    all_functions = set().union(*own_functions.values())
    with engine.connect() as connection:
        for role, allowed in own_functions.items():
            for table in ("public.outbox_events", "public.platform_outbox_events"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not connection.scalar(
                        sa.text(
                            "SELECT has_table_privilege(:role, :table, :privilege)"
                        ),
                        {"role": role, "table": table, "privilege": privilege},
                    )
            for signature in all_functions:
                actual = bool(
                    connection.scalar(
                        sa.text(
                            "SELECT has_function_privilege("
                            ":role, :signature, 'EXECUTE')"
                        ),
                        {"role": role, "signature": signature},
                    )
                )
                assert actual is (signature in allowed)


def test_subs_existing_outbox_authorities_remain_beside_the_module_relay(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        for existing in (
            "public.event_store",
            "public.owner_output_receipts",
            "public.field_erp_sync_events",
            "public.network_operation_dispatches",
        ):
            assert connection.scalar(
                sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": existing}
            ), existing
        assert connection.scalar(
            sa.text("SELECT to_regclass('public.outbox_events') IS NOT NULL")
        )
