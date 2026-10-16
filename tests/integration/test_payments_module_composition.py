"""The tagged Payments tenant lineage composes inertly into Sub.

This is migration and isolation evidence, not a payment-authority switch. Sub's
legacy intent and confirmation writers remain authoritative until their own
backfill, complete shadow comparison and sealed cutover retire them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from dotmac_kernel.cache import TenantScope
from dotmac_payments import GetPaymentIntentV1, get_payment_intent_v1
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import SessionLocal
from app.models.billing import TopupIntent
from app.services import session_hooks
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.payment_intent_module_shadow import (
    AuthorizedInvoiceIntentBinding,
    IntentBindingField,
    IntentParityOutcome,
    authorized_invoice_binding,
    observe_invoice_intent_parity,
)
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database

SCHEMA = "mod_payments"
PAYMENTS_HEAD = "pm_0003_confirmation_immutable"
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
    return set(
        connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    )


def _set_online_scope(connection: sa.Connection, tenant_id: str | None) -> None:
    connection.execute(sa.text("SET LOCAL ROLE app_user"))
    if tenant_id is not None:
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )


def _insert_intent(
    connection: sa.Connection, *, tenant_id: str
) -> AuthorizedInvoiceIntentBinding:
    binding = AuthorizedInvoiceIntentBinding(
        reference=f"composition-{uuid4()}",
        account_id=uuid4(),
        invoice_reference=str(uuid4()),
        requested_amount=Decimal("100.00"),
        currency="NGN",
        provider_type="test",
        channel="customer_selfcare",
        opened_at=datetime.now(UTC),
        expires_at=None,
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO mod_payments.payment_intents (
                id, tenant_id, reference, payer_reference, target_reference,
                purpose, provider_type, channel, currency_code,
                requested_amount, confirmed_amount, minor_units, status, opened_at,
                expires_at, settled_at
            ) VALUES (
                :id, :tenant_id, :reference, :payer_reference, :target_reference,
                'INVOICE_SETTLEMENT', 'TEST', 'CUSTOMER_SELFCARE', 'NGN',
                :requested_amount, NULL, 2, 'PENDING', :opened_at, NULL, NULL
            )
            """
        ),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "reference": binding.reference,
            "payer_reference": str(binding.account_id),
            "target_reference": binding.invoice_reference,
            "requested_amount": Decimal("100.000000"),
            "opened_at": binding.opened_at,
        },
    )

    return binding


def test_payments_lineage_catalog_effective_rls_and_replay(
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
            assert PAYMENTS_HEAD in before_heads
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

        # Seed before denial checks so the read proof is non-vacuous.
        with engine.begin() as connection:
            _set_online_scope(connection, str(OPERATOR_TENANT_ID))
            binding = _insert_intent(connection, tenant_id=str(OPERATOR_TENANT_ID))
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_payments.payment_intents")
                )
                == 1
            )

        with engine.begin() as connection:
            _set_online_scope(connection, None)
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_payments.payment_intents")
                )
                == 0
            )

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

        # Use an app_user connection and the actual root Session after_begin
        # hook. There is deliberately no manual tenant SET in this canary.
        engine.dispose()

        @sa.event.listens_for(engine, "connect")
        def runtime_role(dbapi_connection, _connection_record) -> None:
            with dbapi_connection.cursor() as cursor:
                cursor.execute("SET ROLE app_user")
            dbapi_connection.commit()

        try:
            session_hooks.install_session_hooks()
            assert binding.invoice_reference is not None
            with SessionLocal(bind=engine) as db:
                dirty = TopupIntent(
                    reference=binding.reference,
                    account_id=binding.account_id,
                    invoice_id=UUID(binding.invoice_reference),
                    requested_amount=binding.requested_amount,
                    currency=binding.currency,
                    provider_type=binding.provider_type,
                    channel=binding.channel,
                    created_at=binding.opened_at,
                    expires_at=binding.expires_at,
                    metadata_={"invoice_id": str(uuid4())},
                )
                db.add(dirty)
                assert (
                    observe_invoice_intent_parity(
                        db, authorized_invoice_binding(dirty)
                    ).outcome
                    is IntentParityOutcome.MATCH
                )
                assert dirty in db.new
                assert db.scalar(sa.text("SELECT current_user")) == "app_user"
                assert db.scalar(
                    sa.text("SELECT current_setting('app.current_tenant')")
                ) == str(OPERATOR_TENANT_ID)
                assert (
                    get_payment_intent_v1(
                        db,
                        scope=TenantScope(uuid4()),
                        query=GetPaymentIntentV1(binding.reference),
                    )
                    is None
                )
                assert (
                    observe_invoice_intent_parity(
                        db, replace(binding, reference="missing-module-intent")
                    ).outcome
                    is IntentParityOutcome.MISSING
                )
                mismatch = observe_invoice_intent_parity(
                    db,
                    replace(
                        binding,
                        invoice_reference=str(uuid4()),
                        requested_amount=Decimal("100.000001"),
                    ),
                )
                assert mismatch.mismatched_fields == (
                    IntentBindingField.TARGET,
                    IntentBindingField.AMOUNT,
                )
                assert dirty in db.new
                unbound = authorized_invoice_binding(dirty)
                assert observe_invoice_intent_parity(
                    db, replace(unbound, invoice_reference=None)
                ).mismatched_fields == (IntentBindingField.TARGET,)
                db.rollback()
        finally:
            sa.event.remove(engine, "connect", runtime_role)
            engine.dispose()

        command.upgrade(config, "heads")
        with engine.connect() as connection:
            assert _heads(connection) == before_heads
    finally:
        engine.dispose()
