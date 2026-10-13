"""Exercise released numbering against fresh and real-predecessor migrated PG.

Synthetic series here are fixture data only; no product caller or series is moved.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.cache import TenantScope
from dotmac_numbering.models import PLATFORM_TABLES, TENANT_TABLES, SeriesRepair
from dotmac_numbering.service import (
    ALLOCATE_SCOPE,
    Allocation,
    SeriesConfiguration,
    allocate,
    configure_series,
    preview,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.services import numbering as legacy_numbering
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration import test_payments_module_composition as payments_rehearsal

isolated_database = payments_rehearsal.isolated_database
_config = payments_rehearsal._config
_heads = payments_rehearsal._heads

SCHEMA = "mod_numbering"
HEAD = "nu_0001_numbering"
PREDECESSOR = "556_idempotency_ledger_prereq"
SECOND_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
REFERENCE_DATE = date(2026, 10, 13)
SERIES_CODE = "composition-invoice"


def _scope(connection: sa.Connection, tenant_id: UUID | None) -> None:
    connection.execute(sa.text("SET LOCAL ROLE app_user"))
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant', :tenant_id, true)"),
        {"tenant_id": str(tenant_id) if tenant_id else ""},
    )
    assert connection.scalar(sa.text("SELECT current_user")) == "app_user"


def _counts(connection: sa.Connection) -> tuple[int, ...]:
    return tuple(
        connection.scalar(
            sa.select(sa.func.count()).select_from(sa.table(table, schema=SCHEMA))
        )
        for table in TENANT_TABLES
    ) + (
        connection.scalar(
            sa.text(
                "SELECT count(*) FROM public.idempotency_records WHERE scope=:scope"
            ),
            {"scope": ALLOCATE_SCOPE},
        ),
    )


def _allocate(db: Session, key: str) -> Allocation:
    return allocate(
        db,
        scope=TenantScope(tenant_id=OPERATOR_TENANT_ID),
        series_code=SERIES_CODE,
        reference_date=REFERENCE_DATE,
        idempotency_key=key,
    )


@pytest.mark.parametrize("starting_revision", (None, PREDECESSOR))
def test_numbering_selected_catalogue_rls_allocation_rollback_and_replay(
    isolated_database: URL,
    starting_revision: str | None,
) -> None:
    config = _config(isolated_database)
    engine = sa.create_engine(isolated_database)
    predecessor_record = uuid4()
    try:
        if starting_revision:
            command.upgrade(config, starting_revision)
            with engine.begin() as connection:
                assert _heads(connection) == {PREDECESSOR}
                assert SCHEMA not in sa.inspect(connection).get_schema_names()
                connection.execute(
                    sa.text(
                        "INSERT INTO public.idempotency_records "
                        "(id, tenant_id, scope, key, fingerprint, operation, status) "
                        "VALUES (:id, :tenant, 'numbering.predecessor', 'proof', "
                        "'fixed-fingerprint', 'compose', 'executed')"
                    ),
                    {"id": predecessor_record, "tenant": OPERATOR_TENANT_ID},
                )
        command.upgrade(config, "heads")
        with engine.begin() as connection:
            before_heads = _heads(connection)
            assert HEAD in before_heads
            tables = set(sa.inspect(connection).get_table_names(schema=SCHEMA))
            assert tables == set(TENANT_TABLES)
            assert not tables.intersection(PLATFORM_TABLES)
            posture = {
                row.relname: (row.relrowsecurity, row.relforcerowsecurity)
                for row in connection.execute(
                    sa.text(
                        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=:schema AND c.relkind='r'"
                    ),
                    {"schema": SCHEMA},
                )
            }
            assert posture == dict.fromkeys(TENANT_TABLES, (True, True))
            assert _counts(connection) == (0, 0, 0, 0, 0)
            if starting_revision:
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT fingerprint FROM public.idempotency_records WHERE id=:id"
                        ),
                        {"id": predecessor_record},
                    )
                    == "fixed-fingerprint"
                )
            connection.execute(
                sa.text(
                    "INSERT INTO public.tenants (id, slug, name, is_active) "
                    "VALUES (:id, 'numbering-canary', 'Numbering canary', true)"
                ),
                {"id": SECOND_TENANT_ID},
            )

        for tenant in (OPERATOR_TENANT_ID, SECOND_TENANT_ID):
            with engine.begin() as connection:
                _scope(connection, tenant)
                with Session(bind=connection) as db:
                    # The global Session hook installs Sub's operator scope.
                    # Override only this synthetic tenant fixture after it fires.
                    _scope(db.connection(), tenant)
                    configure_series(
                        db,
                        scope=TenantScope(tenant_id=tenant),
                        configuration=SeriesConfiguration(
                            series_code=SERIES_CODE,
                            prefix="INV",
                            separator="",
                            min_digits=6,
                            start_value=41,
                        ),
                    )

        for tenant in (OPERATOR_TENANT_ID, SECOND_TENANT_ID, None):
            with engine.begin() as connection:
                _scope(connection, tenant)
                assert connection.scalar(
                    sa.text("SELECT count(*) FROM mod_numbering.number_series")
                ) == (0 if tenant is None else 1)
                if tenant:
                    assert (
                        connection.scalar(
                            sa.text("SELECT tenant_id FROM mod_numbering.number_series")
                        )
                        == tenant
                    )

        # Existing visible rows prove both UPDATE filtering and INSERT WITH CHECK.
        for tenant in (SECOND_TENANT_ID, None):
            with engine.connect() as connection:
                transaction = connection.begin()
                _scope(connection, tenant)
                result = connection.execute(
                    sa.text(
                        "UPDATE mod_numbering.number_series SET start_value=99 "
                        "WHERE tenant_id=:tenant"
                    ),
                    {"tenant": OPERATOR_TENANT_ID},
                )
                assert result.rowcount == 0
                with pytest.raises(DBAPIError):
                    connection.execute(
                        sa.text(
                            "INSERT INTO mod_numbering.series_counters "
                            "(id, tenant_id, series_code, period_key, next_value) "
                            "VALUES (:id, :tenant, 'wrong-write', '*', 1)"
                        ),
                        {"id": uuid4(), "tenant": OPERATOR_TENANT_ID},
                    )
                transaction.rollback()

        with engine.begin() as connection:
            _scope(connection, OPERATOR_TENANT_ID)
            with Session(bind=connection) as db:
                before_preview = _counts(connection)
                assert (
                    preview(
                        db,
                        scope=TenantScope(tenant_id=OPERATOR_TENANT_ID),
                        series_code=SERIES_CODE,
                        reference_date=REFERENCE_DATE,
                    )
                    == "INV000041"
                )
                assert _counts(connection) == before_preview
                first = _allocate(db, "exact-retry")
                replay = _allocate(db, "exact-retry")
                assert first.formatted_number == replay.formatted_number == "INV000041"
                assert first.value == replay.value == 41
                assert not first.replayed and replay.replayed
                assert _counts(connection) == (1, 1, 1, 0, 1)

        with engine.connect() as connection:
            transaction = connection.begin()
            _scope(connection, OPERATOR_TENANT_ID)
            with Session(bind=connection) as db:
                assert _allocate(db, "caller-rollback").value == 42
                assert _counts(connection) == (1, 1, 2, 0, 2)
            transaction.rollback()

        with engine.begin() as connection:
            _scope(connection, OPERATOR_TENANT_ID)
            with Session(bind=connection) as db:
                assert _counts(connection) == (1, 1, 1, 0, 1)
                after_rollback = _allocate(db, "caller-rollback")
                assert after_rollback.value == 42
                assert not after_rollback.replayed
                assert _counts(connection) == (1, 1, 2, 0, 2)
        # The incumbent writer retains its own existing database role contract.
        with engine.begin() as connection:
            with Session(bind=connection) as db:
                assert (
                    legacy_numbering.generate_number_with_config(
                        db,
                        "numbering-composition-legacy",
                        prefix="INV-",
                        padding=6,
                        start_value=7,
                    )
                    == "INV-000007"
                )
                assert (
                    legacy_numbering.generate_number_with_config(
                        db,
                        "numbering-composition-legacy",
                        prefix="INV-",
                        padding=6,
                        start_value=7,
                    )
                    == "INV-000008"
                )

        command.upgrade(config, "heads")
        with engine.begin() as connection:
            assert _heads(connection) == before_heads
            _scope(connection, OPERATOR_TENANT_ID)
            with Session(bind=connection) as db:
                assert _counts(connection) == (1, 1, 2, 0, 2)
                assert (
                    preview(
                        db,
                        scope=TenantScope(tenant_id=OPERATOR_TENANT_ID),
                        series_code=SERIES_CODE,
                        reference_date=REFERENCE_DATE,
                    )
                    == "INV000043"
                )
        # Populate every selected table before proving effective per-table isolation.
        for tenant in (OPERATOR_TENANT_ID, SECOND_TENANT_ID):
            with engine.begin() as connection:
                with Session(bind=connection) as db:
                    _scope(db.connection(), tenant)
                    if tenant == SECOND_TENANT_ID:
                        allocate(
                            db,
                            scope=TenantScope(tenant_id=tenant),
                            series_code=SERIES_CODE,
                            reference_date=REFERENCE_DATE,
                            idempotency_key="second-tenant-isolation",
                        )
                    db.add(
                        SeriesRepair(
                            tenant_id=tenant,
                            series_code=SERIES_CODE,
                            period_key="*",
                            previous_next_value=41,
                            new_next_value=41,
                            proven_minimum=41,
                            reason="Synthetic isolation evidence only",
                            repaired_by="composition-canary",
                        )
                    )
                    db.flush()
        for tenant in (OPERATOR_TENANT_ID, SECOND_TENANT_ID, None):
            with engine.begin() as connection:
                _scope(connection, tenant)
                for table in TENANT_TABLES:
                    table_ref = sa.table(table, sa.column("tenant_id"), schema=SCHEMA)
                    visible = set(
                        connection.execute(sa.select(table_ref.c.tenant_id)).scalars()
                    )
                    assert visible == ({tenant} if tenant else set()), table
    finally:
        engine.dispose()
