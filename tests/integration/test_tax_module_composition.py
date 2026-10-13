"""Released Tax on real Sub migration clones; every policy value is synthetic."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from dotmac_kernel.money import Money, currency
from dotmac_tax.contracts import (
    TaxAuthorityInput,
    TaxFact,
    TaxJurisdictionInput,
    TaxRuleInput,
)
from dotmac_tax.models import TENANT_TABLES
from dotmac_tax.service import (
    determine_tax_set,
    ensure_tax_authority,
    ensure_tax_code,
    ensure_tax_jurisdiction,
    ensure_tax_rule,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration.test_payments_module_composition import _config, _heads

SECOND_TENANT = UUID("22222222-2222-4222-8222-222222222222")
PREDECESSOR = "556_idempotency_ledger_prereq"
HEAD = "tx_0003_result_fingerprint"
TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _scope(connection: sa.Connection, tenant: UUID | None) -> None:
    connection.execute(sa.text("SET LOCAL ROLE app_user"))
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(tenant) if tenant else ""},
    )
    assert connection.scalar(sa.text("SELECT current_user")) == "app_user"
    assert connection.execute(
        sa.text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
    ).one() == (False, False)


def _fixture_fact(db: Session, tenant: UUID, source: str) -> TaxFact:
    """Use released public typed APIs, with no product policy/classification."""
    unit = currency("USD")
    authority_command = TaxAuthorityInput(code="fixture", name="Synthetic authority")
    authority = ensure_tax_authority(db, tenant_id=tenant, command=authority_command)
    assert (
        ensure_tax_authority(db, tenant_id=tenant, command=authority_command)
        == authority
    )
    jurisdiction = ensure_tax_jurisdiction(
        db,
        tenant_id=tenant,
        command=TaxJurisdictionInput(
            authority_id=authority.authority_id,
            code="fixture",
            name="Synthetic jurisdiction",
            country_code="US",
            currency=unit,
        ),
    )
    code = ensure_tax_code(
        db,
        tenant_id=tenant,
        jurisdiction_id=jurisdiction.jurisdiction_id,
        code="fixture",
        name="Synthetic code",
        tax_kind_code="composition_fixture",
    )
    rule_command = TaxRuleInput(
        tax_code_id=code.tax_code_id,
        version=1,
        effective_from=TODAY,
        effective_to=None,
        priority=1,
        fact_kind="composition_fixture",
        recognition_basis_code="fixture",
        transaction_side="output",
        calculation_method="percentage",
        rate=Decimal("0.10"),
        fixed_amount=None,
        inclusive=False,
        recoverable_rate=Decimal("0"),
    )
    rule = ensure_tax_rule(db, tenant_id=tenant, command=rule_command)
    assert ensure_tax_rule(db, tenant_id=tenant, command=rule_command) == rule
    return TaxFact(
        jurisdiction_id=jurisdiction.jurisdiction_id,
        occurred_on=TODAY,
        fact_kind="composition_fixture",
        recognition_basis_code="fixture",
        transaction_side="output",
        base_amount=Money(Decimal("100.00"), unit),
        source_ref=source,
        source_version="1",
        evidence_ref="synthetic-composition-only",
    )


@pytest.mark.parametrize("starting_revision", ("base", PREDECESSOR))
def test_tax_real_chain_roles_isolation_retry_and_rollback(
    cloned_database: Callable[[str], URL], starting_revision: str
) -> None:
    url = cloned_database(starting_revision)
    config = _config(url)
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            assert "mod_tax" not in sa.inspect(connection).get_schema_names()
            if starting_revision == PREDECESSOR:
                assert _heads(connection) == {PREDECESSOR}
                connection.execute(
                    sa.text(
                        "INSERT INTO public.idempotency_records "
                        "(id,tenant_id,scope,key,fingerprint,operation,status) "
                        "VALUES (:id,:tenant,'tax.predecessor','fixture',"
                        "'unchanged-proof','compose','executed')"
                    ),
                    {"id": SECOND_TENANT, "tenant": OPERATOR_TENANT_ID},
                )
        command.upgrade(config, "heads")
        with engine.begin() as connection:
            heads = _heads(connection)
            assert HEAD in heads
            if starting_revision == PREDECESSOR:
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT fingerprint FROM public.idempotency_records WHERE id=:id"
                        ),
                        {"id": SECOND_TENANT},
                    )
                    == "unchanged-proof"
                )
            assert set(sa.inspect(connection).get_table_names(schema="mod_tax")) == set(
                TENANT_TABLES
            )
            posture = {
                row.relname: (row.relrowsecurity, row.relforcerowsecurity)
                for row in connection.execute(
                    sa.text(
                        "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname='mod_tax' AND c.relkind='r'"
                    )
                )
            }
            assert posture == dict.fromkeys(TENANT_TABLES, (True, True))
            for table in TENANT_TABLES:
                assert (
                    connection.scalar(
                        sa.select(sa.func.count()).select_from(
                            sa.table(table, schema="mod_tax")
                        )
                    )
                    == 0
                )
            connection.execute(
                sa.text(
                    "INSERT INTO public.tenants(id,slug,name,is_active) "
                    "VALUES (:id,'tax-canary','Synthetic Tax canary',true)"
                ),
                {"id": SECOND_TENANT},
            )
        # Seed both tenants so effective denial cannot pass on empty storage.
        for tenant in (OPERATOR_TENANT_ID, SECOND_TENANT):
            with engine.begin() as connection:
                with Session(bind=connection) as db:
                    _scope(db.connection(), tenant)  # after Sub's global Session hook
                    fact = _fixture_fact(db, tenant, "exact-retry")
                    first = determine_tax_set(
                        db, tenant_id=tenant, fact=fact, determined_at=NOW
                    )
                    replay = determine_tax_set(
                        db, tenant_id=tenant, fact=fact, determined_at=NOW
                    )
                    assert first == replay
                    assert first.tax_amount.amount == Decimal("10.00")
        for tenant in (OPERATOR_TENANT_ID, SECOND_TENANT, None):
            with engine.begin() as connection:
                _scope(connection, tenant)
                for table in (
                    "tax_authorities",
                    "tax_jurisdictions",
                    "tax_codes",
                    "tax_rules",
                    "tax_determination_sets",
                    "tax_determinations",
                    "tax_determination_lines",
                ):
                    relation = sa.table(table, sa.column("tenant_id"), schema="mod_tax")
                    visible = set(
                        connection.execute(sa.select(relation.c.tenant_id)).scalars()
                    )
                    assert visible == ({tenant} if tenant else set()), table
        for tenant in (SECOND_TENANT, None):
            with engine.connect() as connection:
                transaction = connection.begin()
                _scope(connection, tenant)
                denied = connection.execute(
                    sa.text(
                        "UPDATE mod_tax.tax_authorities SET name='forbidden' "
                        "WHERE tenant_id=:tenant"
                    ),
                    {"tenant": OPERATOR_TENANT_ID},
                )
                assert denied.rowcount == 0
                with pytest.raises(DBAPIError):
                    connection.execute(
                        sa.text(
                            "INSERT INTO mod_tax.tax_authorities(id,tenant_id,code,name,status) "
                            "VALUES(gen_random_uuid(),:tenant,'forbidden','Synthetic','active')"
                        ),
                        {"tenant": OPERATOR_TENANT_ID},
                    )
                transaction.rollback()
        with engine.connect() as connection:
            transaction = connection.begin()
            with Session(bind=connection) as db:
                _scope(db.connection(), OPERATOR_TENANT_ID)
                fact = _fixture_fact(db, OPERATOR_TENANT_ID, "caller-rollback")
                rolled_back = determine_tax_set(
                    db, tenant_id=OPERATOR_TENANT_ID, fact=fact, determined_at=NOW
                )
            transaction.rollback()
        with engine.begin() as connection:
            with Session(bind=connection) as db:
                _scope(db.connection(), OPERATOR_TENANT_ID)
                assert (
                    connection.scalar(
                        sa.text("SELECT count(*) FROM mod_tax.tax_determination_sets")
                    )
                    == 1
                )
                fact = _fixture_fact(db, OPERATOR_TENANT_ID, "caller-rollback")
                retry = determine_tax_set(
                    db, tenant_id=OPERATOR_TENANT_ID, fact=fact, determined_at=NOW
                )
                assert retry.determination_set_id != rolled_back.determination_set_id
                assert retry.tax_amount == rolled_back.tax_amount
        command.upgrade(config, "heads")
        with engine.begin() as connection:
            assert _heads(connection) == heads
            _scope(connection, OPERATOR_TENANT_ID)
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_tax.tax_determination_sets")
                )
                == 2
            )
    finally:
        engine.dispose()
