"""The exact-tagged Subscriptions tenant lineage composes inertly into Sub.

This is migration, storage-plane and isolation evidence only. Vendor CP remains
the first authority adopter; Sub's local commercial writers remain authoritative
until a separate backfill, parity and sealed cutover retires them.
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from dotmac_subscriptions.models import PLATFORM_TABLES, TENANT_TABLES
from sqlalchemy.engine import URL

from alembic import command
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database

SCHEMA = "mod_subscriptions"
SUB_PREDECESSOR = "556_idempotency_ledger_prereq"
SUBSCRIPTIONS_HEAD = "su_0003_billing_treatments"
SECOND_TENANT_ID = "22222222-2222-4222-8222-222222222222"


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", _render(database_url))
    return config


def _heads(connection: sa.Connection) -> set[str]:
    return set(
        connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    )


def _seed_two_tenant_offers(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO public.tenants (id, slug, name, is_active) "
            "VALUES (:id, 'subscriptions-canary', 'Subscriptions canary', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": SECOND_TENANT_ID},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO mod_subscriptions.offers (
                id, tenant_id, code, name, description, status
            ) VALUES (
                :first_id, :first_tenant, 'operator-offer', 'Operator offer',
                NULL, 'draft'
            ), (
                :second_id, :second_tenant, 'second-offer', 'Second offer',
                NULL, 'draft'
            )
            """
        ),
        {
            "first_id": uuid4(),
            "first_tenant": str(OPERATOR_TENANT_ID),
            "second_id": uuid4(),
            "second_tenant": SECOND_TENANT_ID,
        },
    )


def _visible_offer_codes(database_url: URL, tenant_id: str | None) -> set[str]:
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE app_user"))
            if tenant_id is not None:
                connection.execute(
                    sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
                    {"tenant": tenant_id},
                )
            return set(
                connection.execute(
                    sa.text("SELECT code FROM mod_subscriptions.offers")
                ).scalars()
            )
    finally:
        engine.dispose()


def test_fresh_upgrade_builds_only_the_selected_tenant_plane_with_effective_rls(
    isolated_database: URL,
) -> None:
    config = _config(isolated_database)
    command.upgrade(config, "heads")

    engine = sa.create_engine(isolated_database)
    try:
        inspector = sa.inspect(engine)
        assert set(inspector.get_table_names(schema=SCHEMA)) == set(TENANT_TABLES)
        assert not set(PLATFORM_TABLES) & set(inspector.get_table_names(schema=SCHEMA))

        with engine.begin() as connection:
            assert SUBSCRIPTIONS_HEAD in _heads(connection)
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
                    {"schema": SCHEMA, "tables": list(TENANT_TABLES)},
                )
            }
            assert posture == dict.fromkeys(TENANT_TABLES, (True, True))
            _seed_two_tenant_offers(connection)

        assert _visible_offer_codes(isolated_database, str(OPERATOR_TENANT_ID)) == {
            "operator-offer"
        }
        assert _visible_offer_codes(isolated_database, SECOND_TENANT_ID) == {
            "second-offer"
        }
        assert _visible_offer_codes(isolated_database, None) == set()

        before = None
        with engine.connect() as connection:
            before = _heads(connection)
        command.upgrade(config, "heads")
        with engine.connect() as connection:
            assert _heads(connection) == before
    finally:
        engine.dispose()


def test_upgrade_from_sub_provider_predecessor_preserves_provider_storage(
    isolated_database: URL,
) -> None:
    config = _config(isolated_database)
    command.upgrade(config, SUB_PREDECESSOR)

    engine = sa.create_engine(isolated_database)
    record_id = uuid4()
    try:
        with engine.begin() as connection:
            assert _heads(connection) == {SUB_PREDECESSOR}
            connection.execute(
                sa.text(
                    """
                    INSERT INTO public.idempotency_records (
                        id, tenant_id, scope, key, fingerprint, operation, status
                    ) VALUES (
                        :id, :tenant_id, 'subscriptions.predecessor', 'proof',
                        'fixed-fingerprint', 'compose', 'executed'
                    )
                    """
                ),
                {"id": record_id, "tenant_id": str(OPERATOR_TENANT_ID)},
            )
        assert SCHEMA not in sa.inspect(engine).get_schema_names()

        command.upgrade(config, "heads")

        with engine.connect() as connection:
            assert SUBSCRIPTIONS_HEAD in _heads(connection)
            preserved = connection.execute(
                sa.text(
                    """
                    SELECT scope, key, fingerprint, operation, status
                      FROM public.idempotency_records
                     WHERE id = :id
                    """
                ),
                {"id": record_id},
            ).one()
            assert tuple(preserved) == (
                "subscriptions.predecessor",
                "proof",
                "fixed-fingerprint",
                "compose",
                "executed",
            )
        assert set(sa.inspect(engine).get_table_names(schema=SCHEMA)) == set(
            TENANT_TABLES
        )
    finally:
        engine.dispose()
