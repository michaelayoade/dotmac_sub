"""Hosted PostgreSQL: real predecessor upgrade, actual login, immutable binding.

No create_all, stamped schema, fabricated module tables, or injected grants.
The unique ephemeral login inherits only migration-supplied app_user grants.
"""

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from dotmac_kernel.models import Tenant
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.models.billing import Invoice
from app.models.event_store import EventStore
from app.models.subscriber import Reseller, Subscriber
from app.services.operator_tenant import operator_tenant_id
from app.services.payments_intent_schema_floor import (
    legacy_image_rollback_allowed,
    read_payments_intent_schema_floor,
)
from scripts.ci.migrated_test_database import (
    effective_heads,
    install_migration_graph_environment,
)
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database


@dataclass(frozen=True)
class MigratedIntentDatabase:
    admin: Engine
    runtime: Engine
    account_id: UUID
    invoice_id: UUID
    historical_id: UUID


_LOCAL_INSERT = """
INSERT INTO public.topup_intents
 (id,account_id,invoice_id,reference,provider_type,currency,requested_amount,status,
  module_intent_id,module_intent_tenant_id,created_at,updated_at)
VALUES (:id,:account,:invoice,:reference,:provider,'NGN',100,'pending',:module,:tenant,now(),now())
"""
_MODULE_INSERT = """
INSERT INTO mod_payments.payment_intents
 (id,tenant_id,reference,payer_reference,target_reference,purpose,provider_type,
 channel,currency_code,requested_amount,status,opened_at)
VALUES (:id,:tenant,:reference,:account,:invoice,'INVOICE_SETTLEMENT','paystack',
 'customer_selfcare','NGN',100,'PENDING',now())
"""


@contextmanager
def _actual_runtime_login(admin: Engine) -> Iterator[Engine]:
    role = f"intent_canary_{uuid4().hex}"
    password = token_urlsafe(32)
    maintenance = admin.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    with psycopg.connect(maintenance, autocommit=True) as connection:
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
        connection.execute(sql.SQL("GRANT app_user TO {}").format(sql.Identifier(role)))
    runtime = create_engine(admin.url.set(username=role, password=password))
    try:
        yield runtime
    finally:
        runtime.dispose()
        with psycopg.connect(maintenance, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def migrated_intents(cloned_database) -> Iterator[MigratedIntentDatabase]:
    url = cloned_database("690_backdated_invoice_permissions")
    admin = create_engine(url)
    historical = uuid4()
    with Session(admin) as db:
        reseller = Reseller(name=f"Payments Intent Canary {uuid4()}", is_active=True)
        account = Subscriber(
            first_name="Canary",
            last_name="Scope",
            email=f"{uuid4()}@example.invalid",
            reseller=reseller,
        )
        db.add(account)
        db.flush()
        invoice = Invoice(account_id=account.id)
        db.add(invoice)
        db.flush()
        account_id, invoice_id = account.id, invoice.id
        db.commit()
    with admin.begin() as connection:
        # The squashed base renders current ORM metadata. Remove only future
        # nullable columns to reproduce the actually installed predecessor.
        connection.execute(
            text(
                "ALTER TABLE public.topup_intents DROP COLUMN IF EXISTS module_intent_id"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE public.topup_intents DROP COLUMN IF EXISTS module_intent_tenant_id"
            )
        )
        connection.execute(
            text(
                'INSERT INTO public.topup_intents (id,account_id,invoice_id,reference,provider_type,status,currency,requested_amount,created_at,updated_at,"metadata") '
                "VALUES (:id,:account,:invoice,'historical-invoice','paystack','pending','NGN',100,now(),now(),"
                '\'{"payment_flow":"invoice_payment"}\'::jsonb)'
            ),
            {"id": historical, "account": account_id, "invoice": invoice_id},
        )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "heads")
    try:
        with _actual_runtime_login(admin) as runtime:
            yield MigratedIntentDatabase(
                admin, runtime, account_id, invoice_id, historical
            )
    finally:
        admin.dispose()


def _scope(db: Session, tenant: UUID | None = None) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(tenant or operator_tenant_id())},
    )


def _parameters(
    database: MigratedIntentDatabase, identity: UUID
) -> dict[str, UUID | str | None]:
    return {
        "id": identity,
        "module": identity,
        "tenant": operator_tenant_id(),
        "account": database.account_id,
        "invoice": database.invoice_id,
        "reference": str(identity),
        "provider": "paystack",
    }


def _linked(db: Session, database: MigratedIntentDatabase, identity: UUID) -> None:
    parameters = _parameters(database, identity)
    db.execute(
        text(_MODULE_INSERT),
        {
            **parameters,
            "account": str(database.account_id),
            "invoice": str(database.invoice_id),
        },
    )
    db.execute(text(_LOCAL_INSERT), parameters)


def test_real_upgrade_preserves_history_and_refuses_old_invoice_writer(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        observed = read_payments_intent_schema_floor(db)
        assert observed.installed_shape_complete
        assert not legacy_image_rollback_allowed(observed)
        assert (
            observed.correlation_columns,
            observed.correlation_constraints,
            observed.correlation_triggers,
            observed.correlation_functions,
        ) == (2, 2, 1, 1)
        db.execute(
            text(
                "UPDATE public.topup_intents SET status='failed', gateway_observation_count=1 WHERE id=:id"
            ),
            {"id": migrated_intents.historical_id},
        )
        assert (
            db.scalar(
                text("SELECT module_intent_id FROM public.topup_intents WHERE id=:id"),
                {"id": migrated_intents.historical_id},
            )
            is None
        )
        db.commit()
        _scope(db)
        with pytest.raises(DBAPIError, match="requires Payments correlation"):
            db.execute(
                text(_LOCAL_INSERT),
                {
                    **_parameters(migrated_intents, uuid4()),
                    "module": None,
                    "tenant": None,
                },
            )
        db.rollback()


@pytest.mark.parametrize(
    "mutation",
    [
        "invoice_id=:other_invoice",
        "reference='fresh-checkout-reference'",
        "account_id=gen_random_uuid()",
        "requested_amount=101",
        "expires_at=now()+interval '1 day'",
        "created_at=created_at+interval '1 day'",
        "provider_type='flutterwave'",
        "provider_type='direct_bank_transfer'",
        "provider_type=NULL",
        "channel='customer_selfcare'",
        "capability_binding_id=gen_random_uuid()",
        '"metadata"=\'{"payment_flow":"reseller_card"}\'::jsonb',
        "\"metadata\"=jsonb_build_object('payment_flow','invoice_payment','invoice_id',CAST(:other_invoice AS text))",
    ],
)
def test_historical_invoice_binding_cannot_be_repurposed(
    migrated_intents: MigratedIntentDatabase,
    mutation: str,
) -> None:
    with Session(migrated_intents.admin) as setup:
        other = Invoice(account_id=migrated_intents.account_id)
        setup.add(other)
        setup.flush()
        other_invoice = other.id
        setup.commit()
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        with pytest.raises(DBAPIError, match="binding is immutable"):
            db.execute(
                text(f"UPDATE public.topup_intents SET {mutation} WHERE id=:id"),  # noqa: S608 - fixed parametrized test SQL only
                {"id": migrated_intents.historical_id, "other_invoice": other_invoice},
            )


def test_historical_invoice_lifecycle_allowed_and_delete_refused(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        db.execute(
            text(
                "UPDATE public.topup_intents SET status='completed',completed_at=now(),"
                "gateway_observation_count=gateway_observation_count+1,"
                "gateway_last_observed_at=now(),"
                'metadata=metadata || \'{"gateway_verification":{"outcome":"success"}}\'::jsonb WHERE id=:id'
            ),
            {"id": migrated_intents.historical_id},
        )
        db.commit()
        _scope(db)
        with pytest.raises(DBAPIError):
            db.execute(
                text("DELETE FROM public.topup_intents WHERE id=:id"),
                {"id": migrated_intents.historical_id},
            )
    # The actual login has no DELETE grant. Independently prove the installed
    # guard protects historical identity even for an elevated legacy caller.
    with Session(migrated_intents.admin) as db:
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            db.execute(
                text("DELETE FROM public.topup_intents WHERE id=:id"),
                {"id": migrated_intents.historical_id},
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "module_intent_id=NULL,module_intent_tenant_id=NULL",
        "module_intent_id=gen_random_uuid()",
        "reference='changed'",
        "invoice_id=NULL",
        "requested_amount=101",
        "account_id=NULL",
        '"metadata"=\'{"payment_method_id":"changed"}\'::jsonb',
    ],
)
def test_linked_binding_is_immutable(
    migrated_intents: MigratedIntentDatabase, mutation: str
) -> None:
    identity = uuid4()
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        _linked(db, migrated_intents, identity)
        db.commit()
        _scope(db)
        with pytest.raises(DBAPIError, match="binding is immutable"):
            db.execute(
                text(f"UPDATE public.topup_intents SET {mutation} WHERE id=:id"),  # noqa: S608 - fixed parametrized test SQL only
                {"id": identity},
            )
        db.rollback()
        _scope(db)
        db.execute(
            text(
                "UPDATE public.topup_intents SET status='completed', completed_at=now(), gateway_observation_count=1 WHERE id=:id"
            ),
            {"id": identity},
        )
        db.commit()
    # DELETE privilege is deliberately absent for app_user; the guard must also
    # refuse an elevated legacy/delete caller rather than relying on that grant.
    with Session(migrated_intents.admin) as db:
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            db.execute(
                text("DELETE FROM public.topup_intents WHERE id=:id"), {"id": identity}
            )


def test_orphan_partial_and_wrong_tenant_links_refused(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    for override in ({}, {"module": None}, {"tenant": None}, {"tenant": uuid4()}):
        with Session(migrated_intents.runtime) as db:
            _scope(db)
            with pytest.raises(DBAPIError):
                db.execute(
                    text(_LOCAL_INSERT),
                    {**_parameters(migrated_intents, uuid4()), **override},
                )


def test_module_projection_and_event_rollback_together(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    identity, event_id = uuid4(), uuid4()
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        _linked(db, migrated_intents, identity)
        db.add(
            EventStore(
                event_id=event_id, event_type="canary.invoice_intent", payload={}
            )
        )
        db.flush()
        db.rollback()
        _scope(db)
        for query, value in (
            (
                "SELECT count(*) FROM mod_payments.payment_intents WHERE id=:id",
                identity,
            ),
            ("SELECT count(*) FROM public.topup_intents WHERE id=:id", identity),
            ("SELECT count(*) FROM public.event_store WHERE event_id=:id", event_id),
        ):
            assert db.scalar(text(query), {"id": value}) == 0


def test_migration_supplies_exact_runtime_read_and_lock_privileges(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        for query, identity in (
            (
                "SELECT id FROM public.subscribers WHERE id=:id FOR NO KEY UPDATE",
                migrated_intents.account_id,
            ),
            (
                "SELECT id FROM public.invoices WHERE id=:id FOR UPDATE",
                migrated_intents.invoice_id,
            ),
        ):
            assert db.scalar(text(query), {"id": identity}) == identity
        for table in (
            "integration_capability_bindings",
            "integration_installations",
            "domain_settings",
        ):
            assert db.scalar(
                text("SELECT has_table_privilege(current_user,:table,'SELECT')"),
                {"table": f"public.{table}"},
            )
        assert not db.scalar(
            text(
                "SELECT has_column_privilege(current_user,'public.invoices','balance_due','UPDATE')"
            )
        )
        assert not db.scalar(
            text(
                "SELECT has_column_privilege(current_user,'public.subscribers','first_name','UPDATE')"
            )
        )


def test_direct_transfer_and_noninvoice_gateway_rows_remain_unlinked(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        for provider, invoice in (
            ("direct_bank_transfer", migrated_intents.invoice_id),
            ("paystack", None),
        ):
            parameters = {
                **_parameters(migrated_intents, uuid4()),
                "module": None,
                "tenant": None,
                "provider": provider,
                "invoice": invoice,
            }
            db.execute(text(_LOCAL_INSERT), parameters)
        db.commit()


@pytest.mark.parametrize(
    ("initial_provider", "initial_invoice", "mutation"),
    [
        ("paystack", False, "invoice_id=:invoice"),
        ("paystack", False, '"metadata"=\'{"payment_flow":"invoice_payment"}\'::jsonb'),
        (
            "paystack",
            False,
            "\"metadata\"=jsonb_build_object('invoice_id',CAST(:invoice AS text))",
        ),
        ("direct_bank_transfer", True, "provider_type='paystack'"),
        (
            "direct_bank_transfer",
            True,
            "provider_type='flutterwave',channel='customer_selfcare'",
        ),
        (
            "direct_bank_transfer",
            False,
            "provider_type='paystack',channel='customer_selfcare',invoice_id=:invoice",
        ),
    ],
)
def test_unlinked_noninvoice_insert_cannot_be_reclassified_into_invoice(
    migrated_intents: MigratedIntentDatabase,
    initial_provider: str,
    initial_invoice: bool,
    mutation: str,
) -> None:
    identity = uuid4()
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        db.execute(
            text(_LOCAL_INSERT),
            {
                **_parameters(migrated_intents, identity),
                "module": None,
                "tenant": None,
                "provider": initial_provider,
                "invoice": migrated_intents.invoice_id if initial_invoice else None,
            },
        )
        # Ordinary unlinked noninvoice lifecycle changes remain admissible.
        db.execute(
            text(
                "UPDATE public.topup_intents SET status='failed',channel='customer_selfcare' WHERE id=:id"
            ),
            {"id": identity},
        )
        db.commit()
        _scope(db)
        with pytest.raises(DBAPIError, match="cannot become gateway invoice intent"):
            db.execute(
                text(f"UPDATE public.topup_intents SET {mutation} WHERE id=:id"),  # noqa: S608 - fixed parametrized test SQL only
                {"id": identity, "invoice": migrated_intents.invoice_id},
            )


def test_actual_login_floor_refuses_partial_and_malformed_catalog(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    with migrated_intents.admin.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE public.topup_intents DISABLE TRIGGER guard_payments_invoice_intent"
            )
        )
    with Session(migrated_intents.runtime) as db:
        observed = read_payments_intent_schema_floor(db)
        assert not observed.installed_shape_complete
        assert not legacy_image_rollback_allowed(observed)

    with migrated_intents.admin.begin() as connection:
        connection.execute(
            text("DROP TRIGGER guard_payments_invoice_intent ON public.topup_intents")
        )
        connection.execute(text("DROP FUNCTION public.guard_payments_invoice_intent()"))
        connection.execute(
            text(
                "ALTER TABLE public.topup_intents DROP CONSTRAINT fk_topup_module_intent_scope"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE public.topup_intents DROP CONSTRAINT ck_topup_module_intent_pair"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE public.topup_intents ALTER COLUMN module_intent_id TYPE text"
            )
        )
    with Session(migrated_intents.runtime) as db:
        observed = read_payments_intent_schema_floor(db)
        assert not observed.installed_shape_complete
        assert not legacy_image_rollback_allowed(observed)


def test_actual_predecessor_runtime_login_admits_incumbent_image(
    cloned_database,
) -> None:
    admin = create_engine(cloned_database("690_backdated_invoice_permissions"))
    try:
        with admin.begin() as connection:
            # Current ORM metadata is rendered by the squashed base. Remove
            # future nullable columns to represent the installed predecessor,
            # before running any 691 column/constraint/function/trigger DDL.
            connection.execute(
                text(
                    "ALTER TABLE public.topup_intents DROP COLUMN IF EXISTS module_intent_id"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE public.topup_intents DROP COLUMN IF EXISTS module_intent_tenant_id"
                )
            )
        with _actual_runtime_login(admin) as runtime, Session(runtime) as db:
            identity = db.execute(text("SELECT session_user,current_user")).one()
            assert identity[0] == identity[1]
            assert identity[0].startswith("intent_canary_")
            observed = read_payments_intent_schema_floor(db)
            assert observed.legacy_shape_complete
            assert not observed.installed_shape_complete
            assert legacy_image_rollback_allowed(observed)
    finally:
        admin.dispose()


def test_installed_floor_refuses_downgrade_before_first_linked_row(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    with Session(migrated_intents.runtime) as db:
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM public.topup_intents WHERE module_intent_id IS NOT NULL"
                )
            )
            == 0
        )
    # Exercise the actual 691 refusal directly: a real downgrade from current
    # heads correctly stops at the later forward-only 693 revision first.
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/691_payments_intent_correlation.py"
    )
    spec = importlib.util.spec_from_file_location("intent_floor_downgrade", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with pytest.raises(RuntimeError, match="floor is permanent"):
        migration.downgrade()
    with Session(migrated_intents.runtime) as db:
        observed = read_payments_intent_schema_floor(db)
        assert observed.installed_shape_complete
        assert not legacy_image_rollback_allowed(observed)


def test_module_rls_hides_foreign_tenant_and_fk_cannot_alias_it(
    migrated_intents: MigratedIntentDatabase,
) -> None:
    other, identity = uuid4(), uuid4()
    with Session(migrated_intents.admin) as db:
        db.add(Tenant(id=other, slug=f"canary-{other}", name="Foreign canary"))
        db.flush()
        parameters = _parameters(migrated_intents, identity)
        db.execute(
            text(_MODULE_INSERT),
            {
                **parameters,
                "tenant": other,
                "account": str(migrated_intents.account_id),
                "invoice": str(migrated_intents.invoice_id),
            },
        )
        db.commit()
    with Session(migrated_intents.runtime) as db:
        _scope(db)
        assert (
            db.scalar(
                text("SELECT count(*) FROM mod_payments.payment_intents WHERE id=:id"),
                {"id": identity},
            )
            == 0
        )
        with pytest.raises(DBAPIError):
            db.execute(text(_LOCAL_INSERT), _parameters(migrated_intents, identity))


def test_fresh_chain_installs_payments_intent_floor_and_replays(
    isolated_database: URL,
) -> None:
    """Prove floor/catalog installation by executing real Alembic from empty."""
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", isolated_database.render_as_string(hide_password=False)
    )
    install_migration_graph_environment()
    script = ScriptDirectory.from_config(config)
    expected = effective_heads(script)
    assert {
        "691_payments_intent_correlation",
        "pm_0003_confirmation_immutable",
    } <= {
        revision.revision
        for revision in script.iterate_revisions(tuple(expected), "base")
    }
    command.upgrade(config, "heads")
    admin = create_engine(isolated_database)
    try:
        with admin.connect() as connection:
            assert (
                frozenset(
                    connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalars()
                )
                == expected
            )
        with _actual_runtime_login(admin) as runtime, Session(runtime) as db:
            observed = read_payments_intent_schema_floor(db)
            assert observed.installed_shape_complete
            assert not legacy_image_rollback_allowed(observed)
        command.upgrade(config, "heads")
        with admin.connect() as connection:
            assert (
                frozenset(
                    connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalars()
                )
                == expected
            )
    finally:
        admin.dispose()
