"""Hosted PostgreSQL entry-only proof; complete source schema, no adoption claim."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from hmac import digest
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.services.operator_tenant import OPERATOR_TENANT_ID
from scripts.migration import kernel_lineage_entry as entry
from scripts.migration.kernel_lineage_rehearsal_canaries import seed_rehearsal_canaries
from scripts.migration.kernel_lineage_rehearsal_evidence import (
    CredentialCohort,
    CredentialPrincipalKind,
    CredentialProvider,
    ProjectionState,
    collect_kernel_lineage_evidence,
)
from tests.integration.test_kernel_lineage_rehearsal import _synthetic_evidence


@pytest.fixture
def engine(cloned_database: Callable[[str], URL]) -> Iterator[Engine]:
    # Replay the real Sub + composed-module Alembic chain; no test-owned
    # schema substitute, including for the historical metadata-built base.
    value = create_engine(cloned_database("heads"))
    try:
        yield value
    finally:
        value.dispose()


@dataclass(frozen=True)
class RowDigest:
    table: str
    count: int
    checksum: str


def _rows(connection: Connection, schema: str) -> tuple[RowDigest, ...]:
    # Fixture-only full-row SQL checksums; do not emit rows or credential values.
    quote = connection.dialect.identifier_preparer.quote
    values: list[RowDigest] = []
    for table in entry.COLLISION_TABLES:
        row = connection.execute(
            text(
                "SELECT count(*),md5(COALESCE(string_agg(row_to_json(t)::text,"  # noqa: S608 - dialect-quoted fixture identifiers
                "'|' ORDER BY row_to_json(t)::text),'')) "
                f"FROM {quote(schema)}.{quote(table)} t"
            )
        ).one()
        values.append(RowDigest(table, int(row[0]), str(row[1])))
    return tuple(values)


@pytest.fixture
def populated(engine: Engine) -> None:
    with Session(engine) as db:
        evidence = _synthetic_evidence(collect_kernel_lineage_evidence(db))
        evidence = evidence.model_copy(
            update={
                "credentials": (
                    *evidence.credentials,
                    CredentialCohort(
                        principal_kind=CredentialPrincipalKind.SYSTEM_USER,
                        provider=CredentialProvider.SSO,
                        projection_state=ProjectionState.LEGACY,
                        is_active=True,
                        has_radius_override=False,
                        count=1,
                    ),
                ),
            }
        )
        # Existing owner creates structural canaries. No provider is converted
        # to a password; NULL external-provider hashes stay NULL in escrow.
        seed_rehearsal_canaries(db, evidence)
    with engine.begin() as connection:
        tenant = OPERATOR_TENANT_ID
        connection.execute(
            text(
                "INSERT INTO tenant_domains "
                "(id,tenant_id,domain,created_at,updated_at) "
                "VALUES (:id,:tenant,'kernel-entry.example.invalid',now(),now())"
            ),
            {"id": uuid4(), "tenant": tenant},
        )
        setting = uuid4()
        connection.execute(
            text(
                "INSERT INTO domain_settings "
                "(id,tenant_id,scope_kind,domain,key,value_type,value_text,is_secret,is_active,created_at,updated_at) "
                "VALUES (:id,:tenant,'tenant','auth','kernel_entry_fixture','string','fixture',false,true,now(),now())"
            ),
            {"id": setting, "tenant": tenant},
        )
        # ORM defaults do not populate this raw SQL history fixture.
        connection.execute(
            text(
                "INSERT INTO domain_setting_history "
                "(id,tenant_id,domain,key,setting_id,action,value_after,"
                "secret_changed,changed_at) "
                "VALUES (:id,:tenant,'auth','kernel_entry_fixture',:setting,"
                "'create','fixture',false,now())"
            ),
            {"id": uuid4(), "tenant": tenant, "setting": setting},
        )
        connection.execute(
            text(
                "INSERT INTO communication_suppressions "
                "(id,channel,address,scope,reason,created_at) "
                "VALUES (:id,'email','kernel-entry@example.invalid','marketing','manual',now())"
            ),
            {"id": uuid4()},
        )
        # A synthetic machine digest is a fixture row, never a human credential
        # disposition or a claim that a live key has been imported.
        connection.execute(
            text(
                "INSERT INTO machine_credentials (id,tenant_id,label,key_hash,scopes) "
                "VALUES (:id,:tenant,'kernel-entry-fixture',:digest,'[]')"
            ),
            {
                "id": uuid4(),
                "tenant": tenant,
                "digest": "hmac-sha256:"
                + digest(
                    b"kernel-entry-fixture-only", b"synthetic-key", "sha256"
                ).hex(),
            },
        )
        for table in ("idempotency_records", "platform_idempotency_records"):
            tenant_column = "tenant_id," if table == "idempotency_records" else ""
            tenant_value = ":tenant," if table == "idempotency_records" else ""
            connection.execute(
                text(
                    f"INSERT INTO {table} ({tenant_column}id,scope,key,operation,status) "  # noqa: S608 - fixed fixture table names
                    f"VALUES ({tenant_value}:id,'fixture','entry','entry','executed')"
                ),
                {"id": uuid4(), "tenant": tenant},
            )
        for table in ("outbox_events", "platform_outbox_events"):
            tenant_column = "tenant_id," if table == "outbox_events" else ""
            tenant_value = ":tenant," if table == "outbox_events" else ""
            connection.execute(
                text(
                    f"INSERT INTO {table} ({tenant_column}id,event_type,payload) "  # noqa: S608 - fixed fixture table names
                    f"VALUES ({tenant_value}:id,'fixture.entry','{{}}')"
                ),
                {"id": uuid4(), "tenant": tenant},
            )
        connection.execute(
            text(
                "UPDATE parties SET status='quarantined' "
                "WHERE display_name LIKE 'kernel-lineage-canary%'"
            )
        )
        connection.exec_driver_sql(
            "CREATE TABLE public.kernel_entry_incoming_fixture ("
            "id uuid PRIMARY KEY,party_id uuid NOT NULL REFERENCES public.parties(id))"
        )
        connection.execute(
            text(
                "INSERT INTO kernel_entry_incoming_fixture SELECT :id,id FROM parties "
                "WHERE display_name LIKE 'kernel-lineage-canary%' ORDER BY id LIMIT 1"
            ),
            {"id": uuid4()},
        )


def test_complete_installed_chain_enters_without_consuming_sub_history(
    engine: Engine,
    populated: None,
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        plan = entry.observe_fixture_plan(connection)
        before = _rows(connection, "public")
        assert all(row.count > 0 for row in before)
        incoming_oid = connection.scalar(
            text(
                "SELECT confrelid FROM pg_constraint "
                "WHERE conrelid='public.kernel_entry_incoming_fixture'::regclass "
                "AND contype='f'"
            )
        )
        try:
            observed = entry.rehearse_kernel_entry(connection, plan=plan)
            assert root.is_active and not connection.in_nested_transaction()
            assert observed.kernel_heads == (entry.KERNEL_HEAD,)
            assert observed.sub_heads == plan.catalog.sub_heads
            assert _rows(connection, entry.ESCROW_SCHEMA) == before
            destinations = {row.name: row for row in observed.canonical_relations}
            assert incoming_oid != destinations["parties"].oid
            assert (
                connection.scalar(
                    text(
                        "SELECT confrelid FROM pg_constraint "
                        "WHERE conrelid='public.kernel_entry_incoming_fixture'::regclass "
                        "AND contype='f'"
                    )
                )
                == incoming_oid
            )
            # Both claims retain the original composite return OIDs; Kernel's
            # newly executed relay revisions must create different functions.
            old = {r.signature: r for r in observed.predecessor_relays}
            for signature in entry.RELAY_SIGNATURES:
                current_oid = connection.scalar(
                    text("SELECT to_regprocedure(:signature)::oid"),
                    {"signature": f"public.{signature}"},
                )
                assert current_oid != old[signature].oid
            for function, table in (
                ("claim_outbox_batch", "outbox_events"),
                ("claim_platform_outbox_batch", "platform_outbox_events"),
            ):
                signature = f"{function}(text,integer,integer)"
                assert (
                    old[signature].result_type_oid != destinations[table].row_type_oid
                )
            for role in (
                "app_user",
                "platform_api",
                "outbox_dispatcher",
                "platform_outbox_dispatcher",
            ):
                assert not connection.scalar(
                    text("SELECT has_schema_privilege(:role,:schema,'USAGE')"),
                    {"role": role, "schema": entry.ESCROW_SCHEMA},
                )
            with pytest.raises(entry.KernelEntryRefused, match="Escrow exists"):
                entry.rehearse_kernel_entry(connection, plan=plan)
        finally:
            root.rollback()
        # Same physical connection: every source OID/ACL/definition and row
        # survives, both histories and the escrow creation have rolled back.
        with connection.begin():
            assert entry.observe_fixture_plan(connection) == plan
            assert _rows(connection, "public") == before


@pytest.mark.parametrize("phase", tuple(entry.EntryPhase))
def test_each_entry_phase_rolls_back_catalog_grants_rows_and_history(
    engine: Engine,
    populated: None,
    phase: entry.EntryPhase,
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        plan = entry.observe_fixture_plan(connection)
        before = _rows(connection, "public")
        try:
            with pytest.raises(entry.InjectedEntryFailure):
                entry.rehearse_kernel_entry(connection, plan=plan, fail_after=phase)
            assert root.is_active
        finally:
            root.rollback()
        with connection.begin():
            assert entry.observe_fixture_plan(connection) == plan
            assert _rows(connection, "public") == before


def test_catalog_drift_cannot_become_a_new_disposition(
    engine: Engine,
    populated: None,
) -> None:
    with engine.connect() as connection, connection.begin():
        plan = entry.observe_fixture_plan(connection)
        connection.exec_driver_sql("GRANT SELECT (id) ON public.roles TO app_user")
        with pytest.raises(entry.KernelEntryRefused, match="catalog changed"):
            entry.rehearse_kernel_entry(connection, plan=plan)
        assert (
            connection.scalar(
                text("SELECT to_regnamespace(:schema)"), {"schema": entry.ESCROW_SCHEMA}
            )
            is None
        )


def test_failure_inside_the_real_kernel_chain_rolls_back_the_complete_entry(
    engine: Engine,
    populated: None,
) -> None:
    # PostgreSQL raises during an actual unchanged 0001 CREATE TABLE, after
    # earlier canonical tables exist. No substituted migration body or stamp.
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE FUNCTION public.reject_kernel_entry_fixture_ddl()
            RETURNS event_trigger LANGUAGE plpgsql AS $$
            DECLARE item record;
            BEGIN
                FOR item IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
                    IF item.object_identity = 'public.auth_sessions' THEN
                        RAISE EXCEPTION 'injected real Kernel DDL failure';
                    END IF;
                END LOOP;
            END;
            $$
        """)
        connection.exec_driver_sql(
            "CREATE EVENT TRIGGER reject_kernel_entry_fixture_ddl "
            "ON ddl_command_end EXECUTE FUNCTION public.reject_kernel_entry_fixture_ddl()"
        )
    with engine.connect() as connection:
        root = connection.begin()
        plan = entry.observe_fixture_plan(connection)
        before = _rows(connection, "public")
        try:
            with pytest.raises(DBAPIError, match="injected real Kernel DDL failure"):
                entry.rehearse_kernel_entry(connection, plan=plan)
        finally:
            root.rollback()
        with connection.begin():
            assert entry.observe_fixture_plan(connection) == plan
            assert _rows(connection, "public") == before


@pytest.mark.parametrize(
    "dependency",
    (
        "view",
        "composite_function",
        "parsed_relation_function",
        "relay_wrapper",
        "transitive_relay_wrapper",
    ),
)
def test_unaccounted_callable_dependency_requires_its_own_disposition(
    engine: Engine,
    populated: None,
    dependency: str,
) -> None:
    with engine.connect() as connection, connection.begin():
        if dependency == "view":
            connection.exec_driver_sql(
                "CREATE VIEW public.unreviewed_entry_view AS SELECT id FROM public.parties"
            )
        elif dependency == "composite_function":
            connection.exec_driver_sql(
                "CREATE FUNCTION public.unreviewed_entry_function() "
                "RETURNS SETOF public.outbox_events LANGUAGE sql "
                "AS 'SELECT * FROM public.outbox_events'"
            )
        elif dependency == "parsed_relation_function":
            connection.exec_driver_sql(
                "CREATE FUNCTION public.unreviewed_entry_count() "
                "RETURNS bigint LANGUAGE sql BEGIN ATOMIC "
                "SELECT count(*) FROM public.parties; END"
            )
            # Prove PostgreSQL recorded the parsed scalar-return body reference;
            # the old composite-return-only guard missed this native edge.
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_depend "
                    "WHERE classid='pg_proc'::regclass "
                    "AND objid='public.unreviewed_entry_count()'::regprocedure "
                    "AND refclassid='pg_class'::regclass "
                    "AND refobjid='public.parties'::regclass)"
                )
            )
        else:
            connection.exec_driver_sql(
                "CREATE FUNCTION public.unreviewed_entry_relay_count() "
                "RETURNS bigint LANGUAGE sql BEGIN ATOMIC "
                "SELECT count(*) FROM public.claim_outbox_batch('fixture',1,30); END"
            )
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_depend "
                    "WHERE classid='pg_proc'::regclass "
                    "AND objid='public.unreviewed_entry_relay_count()'::regprocedure "
                    "AND refclassid='pg_proc'::regclass "
                    "AND refobjid='public.claim_outbox_batch(text,integer,integer)'::regprocedure)"
                )
            )
            if dependency == "transitive_relay_wrapper":
                connection.exec_driver_sql(
                    "CREATE FUNCTION public.unreviewed_entry_outer_count() "
                    "RETURNS bigint LANGUAGE sql BEGIN ATOMIC "
                    "SELECT public.unreviewed_entry_relay_count(); END"
                )
                assert connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_depend "
                        "WHERE classid='pg_proc'::regclass "
                        "AND objid='public.unreviewed_entry_outer_count()'::regprocedure "
                        "AND refclassid='pg_proc'::regclass "
                        "AND refobjid='public.unreviewed_entry_relay_count()'::regprocedure)"
                    )
                )
        with pytest.raises(entry.KernelEntryRefused, match="disposition"):
            entry.observe_fixture_plan(connection)


def test_external_root_is_required_and_nested_or_wrong_artifact_is_refused(
    engine: Engine,
    populated: None,
) -> None:
    with engine.connect() as connection:
        with pytest.raises(entry.KernelEntryRefused, match="root transaction"):
            entry.observe_fixture_plan(connection)
        with connection.begin():
            plan = entry.observe_fixture_plan(connection)
            with connection.begin_nested():
                with pytest.raises(entry.KernelEntryRefused, match="root transaction"):
                    entry.observe_fixture_plan(connection)
            with pytest.raises(entry.KernelEntryRefused, match="typed fixture"):
                entry.rehearse_kernel_entry(
                    connection, plan=replace(plan, kernel_version="0.1.0a97")
                )
