"""The released Payments tenant lineage composes inertly into Sub.

This is migration and isolation evidence, not a payment-authority switch. Sub's
legacy intent and confirmation writers remain authoritative until their own
backfill, complete shadow comparison and sealed cutover retire them. The test
uses the installed ``dotmac-payments==0.1.0a1`` lineage through Sub's real
Alembic environment and exercises FORCE RLS as the effective online role.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database

SCHEMA = "mod_payments"
HEADS = {"pm_0001_payment_intents", "so_0001_service_delivery_orders"}
TABLES = (
    "payment_intents",
    "payment_transfer_proofs",
    "payment_confirmations",
)


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", _render(database_url))
    return config


def _heads(connection: sa.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        )
    }


def _set_online_scope(connection: sa.Connection, tenant_id: str | None) -> None:
    connection.execute(sa.text("SET LOCAL ROLE app_user"))
    if tenant_id is not None:
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )


def _insert_intent(connection: sa.Connection, *, tenant_id: str) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO mod_payments.payment_intents (
                id, tenant_id, reference, payer_reference, target_reference,
                purpose, provider_type, channel, currency_code,
                requested_amount, confirmed_amount, status, opened_at,
                expires_at, settled_at
            ) VALUES (
                :id, :tenant_id, :reference, :payer_reference, NULL,
                'ACCOUNT_CREDIT_DEPOSIT', 'TEST', 'TRANSFER', 'NGN',
                :requested_amount, NULL, 'PENDING', :opened_at, NULL, NULL
            )
            """
        ),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "reference": f"composition-{uuid4()}",
            "payer_reference": "operator-account",
            "requested_amount": Decimal("100.000000"),
            "opened_at": datetime.now(UTC),
        },
    )


def test_payments_lineage_head_catalog_rls_and_replay(
    isolated_database: URL,
) -> None:
    """One migration cost proves the complete inert-composition boundary."""

    config = _config(isolated_database)
    command.upgrade(config, "heads")

    engine = sa.create_engine(isolated_database)
    try:
        inspector = sa.inspect(engine)
        assert set(inspector.get_table_names(schema=SCHEMA)) == set(TABLES)

        with engine.connect() as connection:
            before_heads = _heads(connection)
            assert HEADS <= before_heads

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
                    {"schema": SCHEMA, "tables": list(TABLES)},
                )
            }
            assert posture == dict.fromkeys(TABLES, (True, True))

        # The canonical operator tenant can write and read through app_user.
        # Seed this row before the denial checks so wrong-tenant SELECT is not
        # vacuously green against an empty table.
        with engine.begin() as connection:
            _set_online_scope(connection, str(OPERATOR_TENANT_ID))
            _insert_intent(connection, tenant_id=str(OPERATOR_TENANT_ID))
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_payments.payment_intents")
                )
                == 1
            )

        # No ambient tenant means an online read cannot see the seeded row.
        with engine.begin() as connection:
            _set_online_scope(connection, None)
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_payments.payment_intents")
                )
                == 0
            )

        # A different tenant cannot see the seeded operator row or create one
        # for that tenant identity.
        with engine.connect() as connection:
            transaction = connection.begin()
            _set_online_scope(connection, str(uuid4()))
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_payments.payment_intents")
                )
                == 0
            )
            with pytest.raises(DBAPIError):
                _insert_intent(connection, tenant_id=str(OPERATOR_TENANT_ID))
            transaction.rollback()

        # Deploys intentionally run ``upgrade heads`` repeatedly.
        command.upgrade(config, "heads")
        with engine.connect() as connection:
            assert _heads(connection) == before_heads
    finally:
        engine.dispose()
