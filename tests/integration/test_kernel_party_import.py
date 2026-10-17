"""Hosted real-chain PostgreSQL rollback proof; fixtures are not acceptance."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.party_kernel_import import (
    NoAdoption,
    PartyFixtureDisposition,
    PartyImportCohort,
    PartyImportCount,
    PartyImportRefused,
    observe_party_source,
)
from scripts.migration import kernel_lineage_entry as entry
from scripts.migration import kernel_party_import as adoption
from tests.integration.test_kernel_lineage_entry import (
    _rows,
)
from tests.integration.test_kernel_lineage_entry import (
    engine as migrated_engine_fixture,
)
from tests.integration.test_kernel_lineage_entry import (
    populated as populated_entry_fixture,
)

# Reuse hosted complete-chain fixtures without constructing a second schema.
engine = migrated_engine_fixture
populated = populated_entry_fixture


def _cohort(connection: Connection) -> PartyImportCohort:
    return PartyImportCohort(
        OPERATOR_TENANT_ID,
        tuple(
            PartyFixtureDisposition(
                f,
                OPERATOR_TENANT_ID,
                index % 2 == 0,
                "hosted-synthetic-fixture-review-v1",
                NoAdoption.EMAIL,
                NoAdoption.SUBTYPE,
                NoAdoption.CUSTOM_FIELDS,
            )
            for index, f in enumerate(observe_party_source(connection))
        ),
    )


@pytest.fixture
def exact_parties(engine: Engine, populated: None) -> None:
    with engine.begin() as connection:
        # Every lifecycle/classification/type combination, JSON NULL vs SQL NULL,
        # objects/arrays/scalars/textual formatting, non-trimmed names, exact time.
        first, second = uuid4(), uuid4()
        for index, (status, classification, kind) in enumerate(
            (s, c, k)
            for s in ("active", "quarantined", "merged", "archived")
            for c in ("production", "test", "imported_unverified")
            for k in ("person", "organization")
        ):
            party = first if index == 0 else second if index == 1 else uuid4()
            payload = (
                None,
                "null",
                "{}",
                '[1, null, "x"]',
                '"string"',
                "123",
                '{ "spaced" : 1, "spaced" : 2 }',
            )[index % 7]
            connection.execute(
                text(
                    "INSERT INTO parties (id,party_type,display_name,status,data_classification,"
                    "merged_into_party_id,merge_reason,metadata,created_at,updated_at) "
                    "VALUES (:id,:kind,:name,:status,:classification,:target,:reason,CAST(:metadata AS json),"
                    "'2024-02-03T04:05:06.123456+00','2025-03-04T05:06:07.654321+00')"
                ),
                {
                    "id": party,
                    "kind": kind,
                    "name": f"  fixture {index}  ",
                    "status": status,
                    "classification": classification,
                    "target": first if status == "merged" else None,
                    "reason": "  exact historical reason  "
                    if status == "merged"
                    else None,
                    "metadata": payload,
                },
            )
        # Source-valid merged cycle: all roots must exist before context insert.
        connection.execute(
            text(
                "UPDATE parties SET status='merged',merged_into_party_id=:second,merge_reason='cycle' WHERE id=:first"
            ),
            {"first": first, "second": second},
        )
        connection.execute(
            text(
                "UPDATE parties SET status='merged',merged_into_party_id=:first,merge_reason='cycle' WHERE id=:second"
            ),
            {"first": first, "second": second},
        )
        for kind in ("person", "organization"):
            connection.execute(
                text(
                    "INSERT INTO parties (id,party_type,display_name,status,data_classification,created_at,updated_at) "
                    "VALUES (:id,:kind,'active production fixture','active','production',now(),now())"
                ),
                {"id": uuid4(), "kind": kind},
            )
        connection.execute(
            text(
                "UPDATE tenants SET name='  historical operator  ',is_active=false,"
                "suspended_at='2023-01-01T01:02:03.123456+00',deleted_at='2023-02-01T02:03:04.234567+00',"
                "created_at='2020-01-01T03:04:05.345678+00',updated_at='2023-03-01T04:05:06.456789+00'"
            )
        )


def test_exact_roots_context_and_independent_history_roll_back(
    engine: Engine, exact_parties: None
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        before = entry.observe_fixture_plan(connection)
        rows = _rows(connection, "public")
        cohort = _cohort(connection)
        try:
            result = adoption.rehearse_party_import(
                connection, plan=before, cohort=cohort
            )
            assert root.is_active and not connection.in_nested_transaction()
            assert result.adoption_heads == (adoption.ADOPTION_HEAD,)
            assert (
                result.roots.count == result.contexts.count == len(cohort.dispositions)
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM sub_kernel_entry_fixture_escrow.parties s
                JOIN public.parties p ON p.id=s.id
                JOIN public.sub_party_context c ON c.party_id=s.id
                WHERE (p.party_type,p.display_name,p.created_at,p.updated_at)
                    IS DISTINCT FROM (s.party_type,s.display_name,s.created_at,s.updated_at)
                OR p.tenant_id<>:tenant OR c.tenant_id<>:tenant
                OR p.email IS NOT NULL OR p.custom_fields::text<>'{}'
                OR (c.status,c.data_classification,c.merged_into_party_id,c.merge_reason,c.metadata::text)
                    IS DISTINCT FROM (s.status,s.data_classification,s.merged_into_party_id,s.merge_reason,s.metadata::text)
            """),
                    {"tenant": OPERATOR_TENANT_ID},
                )
                == 0
            )
            for d in cohort.dispositions:
                assert (
                    connection.scalar(
                        text("SELECT is_active FROM public.parties WHERE id=:id"),
                        {"id": d.source.party_id},
                    )
                    is d.is_active
                )
            assert connection.scalar(
                text("""
                SELECT row_to_json(t)::text FROM public.tenants t
            """)
            ) == connection.scalar(
                text("""
                SELECT row_to_json(t)::text FROM sub_kernel_entry_fixture_escrow.tenants t
            """)
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM public.party_persons"))
                == 0
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.party_organizations")
                )
                == 0
            )
            assert tuple(
                connection.scalars(
                    text("""
                SELECT DISTINCT confrelid FROM pg_constraint
                WHERE conrelid='public.sub_party_context'::regclass AND contype='f'
            """)
                )
            ) == (connection.scalar(text("SELECT 'public.parties'::regclass::oid")),)
            assert connection.scalar(
                text("""
                SELECT confrelid FROM pg_constraint
                WHERE conrelid='public.kernel_entry_incoming_fixture'::regclass AND contype='f'
            """)
            ) == connection.scalar(
                text("SELECT 'sub_kernel_entry_fixture_escrow.parties'::regclass::oid")
            )
            assert not connection.scalar(
                text("""
                SELECT EXISTS (SELECT 1 FROM pg_class c
                CROSS JOIN LATERAL aclexplode(c.relacl) a
                WHERE c.oid='public.sub_party_context'::regclass AND a.grantee<>c.relowner)
            """)
            )
            assert connection.scalar(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='public.sub_party_context'::regclass"
                )
            )
        finally:
            root.rollback()
        assert not root.is_active
        assert connection.get_transaction() is None
        with connection.begin() as verification:
            assert verification is not root
            assert connection.get_transaction() is verification
            assert entry.observe_fixture_plan(connection) == before
            assert _rows(connection, "public") == rows
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.sub_party_context')")
                )
                is None
            )
            assert (
                connection.scalar(
                    text(
                        f"SELECT to_regclass('public.{adoption.ADOPTION_VERSION_TABLE}')"
                    )
                )
                is None
            )


@pytest.mark.parametrize("phase", tuple(adoption.PartyImportPhase))
def test_each_import_phase_rolls_back_every_catalog_and_row(
    engine: Engine, exact_parties: None, phase: adoption.PartyImportPhase
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        before = entry.observe_fixture_plan(connection)
        rows = _rows(connection, "public")
        try:
            with pytest.raises(adoption.InjectedPartyImportFailure):
                adoption.rehearse_party_import(
                    connection,
                    plan=before,
                    cohort=_cohort(connection),
                    fail_after=phase,
                )
        finally:
            root.rollback()
        assert not root.is_active
        assert connection.get_transaction() is None
        with connection.begin() as verification:
            assert verification is not root
            assert connection.get_transaction() is verification
            assert entry.observe_fixture_plan(connection) == before
            assert _rows(connection, "public") == rows


@pytest.mark.parametrize(
    "bad",
    (
        "missing",
        "duplicate",
        "stale",
        "unused",
        "tenant",
        "active",
        "provenance",
        "email",
        "subtype",
        "custom_fields",
    ),
)
def test_dispositions_refuse_before_entry(
    engine: Engine, exact_parties: None, bad: str
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        before = entry.observe_fixture_plan(connection)
        good = _cohort(connection)
        first = good.dispositions[0]
        rest = good.dispositions[1:]
        if bad == "missing":
            changed = rest
        elif bad == "duplicate":
            changed = (*good.dispositions, first)
        elif bad == "unused":
            changed = (
                *good.dispositions,
                replace(first, source=replace(first.source, party_id=uuid4())),
            )
        elif bad == "stale":
            changed = (
                replace(first, source=replace(first.source, row_sha256="0" * 64)),
                *rest,
            )
        else:
            value = {
                "tenant": ("tenant_id", uuid4()),
                "active": ("is_active", 1),
                "provenance": ("fixture_provenance", " "),
                "email": ("email", NoAdoption.SUBTYPE),
                "subtype": ("subtype", NoAdoption.EMAIL),
                "custom_fields": ("custom_fields", NoAdoption.EMAIL),
            }[bad]
            changed = (replace(first, **{value[0]: value[1]}), *rest)
        try:
            with pytest.raises(PartyImportRefused):
                adoption.rehearse_party_import(
                    connection, plan=before, cohort=replace(good, dispositions=changed)
                )
            assert entry.observe_fixture_plan(connection) == before
        finally:
            root.rollback()


def test_context_composite_fk_refuses_other_tenant(
    engine: Engine, exact_parties: None
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        try:
            cohort = _cohort(connection)
            adoption.rehearse_party_import(
                connection, plan=entry.observe_fixture_plan(connection), cohort=cohort
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO public.sub_party_context (tenant_id,party_id,status,data_classification) VALUES (:tenant,:party,'active','test')"
                    ),
                    {
                        "tenant": uuid4(),
                        "party": cohort.dispositions[0].source.party_id,
                    },
                )
        finally:
            root.rollback()


@pytest.mark.parametrize(
    "object_name", ("sub_party_context", "sub_kernel_adoption_alembic_version")
)
def test_preexisting_adoption_objects_refuse_and_rollback(
    engine: Engine, exact_parties: None, object_name: str
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        try:
            # Only fixed parametrized fixture identifiers are interpolated.
            connection.exec_driver_sql(
                f"CREATE TABLE public.{object_name} (fixture integer)"
            )
            before = entry.observe_fixture_plan(connection)
            with pytest.raises(PartyImportRefused):
                adoption.rehearse_party_import(
                    connection, plan=before, cohort=_cohort(connection)
                )
        finally:
            root.rollback()
        assert (
            connection.scalar(
                text("SELECT to_regclass(:name)"), {"name": f"public.{object_name}"}
            )
            is None
        )
        assert (
            connection.scalar(
                text("SELECT to_regnamespace(:name)"), {"name": entry.ESCROW_SCHEMA}
            )
            is None
        )


@pytest.mark.parametrize("drift", ("history", "canonical_oid"))
def test_adoption_revalidates_actual_canonical_targets_before_ddl(
    engine: Engine, exact_parties: None, drift: str
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        try:
            observed = entry.rehearse_kernel_entry(
                connection, plan=entry.observe_fixture_plan(connection)
            )
            if drift == "history":
                connection.execute(
                    text(
                        "UPDATE public.dotmac_kernel_alembic_version SET version_num='wrong'"
                    )
                )
            else:
                # Replace the canonical target with escrow, inside this disposable root.
                connection.exec_driver_sql("DROP TABLE public.parties CASCADE")
                connection.exec_driver_sql(
                    "ALTER TABLE sub_kernel_entry_fixture_escrow.parties SET SCHEMA public"
                )
            with pytest.raises(PartyImportRefused):
                adoption._apply_adoption(connection, observed=observed)
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.sub_party_context')")
                )
                is None
            )
            assert (
                connection.scalar(
                    text(
                        f"SELECT to_regclass('public.{adoption.ADOPTION_VERSION_TABLE}')"
                    )
                )
                is None
            )
        finally:
            root.rollback()


def test_populated_context_downgrade_refuses(
    engine: Engine, exact_parties: None
) -> None:
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    with engine.connect() as connection:
        root = connection.begin()
        try:
            cohort = _cohort(connection)
            adoption.rehearse_party_import(
                connection, plan=entry.observe_fixture_plan(connection), cohort=cohort
            )
            config = Config()
            config.set_main_option(
                "script_location",
                str(Path(__file__).resolve().parents[2] / "alembic_kernel_adoption"),
            )
            revision = ScriptDirectory.from_config(config).get_revision(
                adoption.ADOPTION_HEAD
            )
            assert revision is not None
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(
                    RuntimeError, match="Populated Party context downgrade refused"
                ):
                    revision.module.downgrade()
            assert (
                connection.scalar(
                    text("SELECT to_regclass('public.sub_party_context')")
                )
                is not None
            )
        finally:
            root.rollback()


def test_nullable_tenant_history_is_copied_exactly(
    engine: Engine, populated: None
) -> None:
    with engine.connect() as connection:
        root = connection.begin()
        try:
            connection.execute(
                text(
                    "UPDATE tenants SET suspended_at=NULL,deleted_at=NULL,is_active=true"
                )
            )
            before = connection.execute(
                text(
                    "SELECT id,slug,name,is_active,suspended_at,deleted_at,created_at,updated_at FROM tenants"
                )
            ).one()
            cohort = _cohort(connection)
            adoption.rehearse_party_import(
                connection, plan=entry.observe_fixture_plan(connection), cohort=cohort
            )
            assert (
                connection.execute(
                    text(
                        "SELECT id,slug,name,is_active,suspended_at,deleted_at,created_at,updated_at FROM public.tenants"
                    )
                ).one()
                == before
            )
        finally:
            root.rollback()


def test_second_source_tenant_refuses_without_inventing_party_attribution(
    engine: Engine, exact_parties: None
) -> None:
    from app.services.operator_tenant_kernel_import import (
        HistoricalOperatorTenantImportRefused,
    )

    with engine.connect() as connection:
        root = connection.begin()
        try:
            connection.execute(
                text(
                    "INSERT INTO tenants (id,slug,name,is_active,created_at,updated_at) "
                    "VALUES (:id,'second-fixture','Second fixture',true,now(),now())"
                ),
                {"id": uuid4()},
            )
            with pytest.raises(HistoricalOperatorTenantImportRefused):
                adoption.rehearse_party_import(
                    connection,
                    plan=entry.observe_fixture_plan(connection),
                    cohort=_cohort(connection),
                )
        finally:
            root.rollback()
        assert connection.scalar(text("SELECT count(*) FROM public.tenants")) == 1
        assert (
            connection.scalar(
                text("SELECT to_regnamespace(:name)"), {"name": entry.ESCROW_SCHEMA}
            )
            is None
        )


@pytest.mark.parametrize("table", ("tenants", "parties", "sub_party_context"))
@pytest.mark.parametrize("corruption", ("suppress", "modify", "extra"))
def test_insert_trigger_corruption_refuses_and_whole_root_rolls_back(
    engine: Engine,
    exact_parties: None,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    corruption: str,
) -> None:
    from app.services.operator_tenant_kernel_import import (
        HistoricalOperatorTenantImportRefused,
    )

    stage_name = {
        "tenants": "stage_historical_operator_tenant",
        "parties": "stage_party_roots",
        "sub_party_context": "stage_party_context",
    }[table]
    original_tenant = adoption.stage_historical_operator_tenant
    original_roots = adoption.stage_party_roots
    original_context = adoption.stage_party_context

    def corrupting_stage(
        connection: Connection,
        *,
        tenant_id: UUID | None = None,
        cohort: PartyImportCohort | None = None,
    ) -> UUID | PartyImportCount:
        if corruption != "extra":
            mutation = {
                "tenants": "NEW.name := 'trigger-modified-fixture';",
                "parties": "NEW.display_name := 'trigger-modified-fixture';",
                "sub_party_context": "NEW.metadata := 'null'::json;",
            }[table]
            body = (
                "RETURN NULL;"
                if corruption == "suppress"
                else mutation + " RETURN NEW;"
            )
            connection.exec_driver_sql(
                f"CREATE FUNCTION public.party_import_corruption_fixture() RETURNS trigger "
                f"LANGUAGE plpgsql AS $$ BEGIN {body} END; $$"
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER party_import_corruption_fixture BEFORE INSERT ON public.{table} "
                "FOR EACH ROW EXECUTE FUNCTION public.party_import_corruption_fixture()"
            )
        if table == "tenants":
            assert tenant_id is not None
            result = original_tenant(connection, tenant_id=tenant_id)
        else:
            assert cohort is not None
            result = (
                original_roots(connection, cohort=cohort)
                if table == "parties"
                else original_context(connection, cohort=cohort)
            )
        if corruption == "extra":
            if table == "tenants":
                connection.execute(
                    text(
                        "INSERT INTO public.tenants (id,slug,name,is_active,created_at,updated_at) "
                        "VALUES (:id,'extra-fixture','Extra fixture',true,now(),now())"
                    ),
                    {"id": uuid4()},
                )
            elif table == "parties":
                connection.execute(
                    text(
                        "INSERT INTO public.parties (id,tenant_id,party_type,display_name,is_active,custom_fields,created_at,updated_at) "
                        "VALUES (:id,:tenant,'person','Extra fixture',true,'{}',now(),now())"
                    ),
                    {"id": uuid4(), "tenant": OPERATOR_TENANT_ID},
                )
            else:
                # Extra context with a valid FK but absent from the source cohort.
                extra = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO public.parties (id,tenant_id,party_type,display_name,is_active,custom_fields,created_at,updated_at) "
                        "VALUES (:id,:tenant,'person','Extra fixture',true,'{}',now(),now())"
                    ),
                    {"id": extra, "tenant": OPERATOR_TENANT_ID},
                )
                connection.execute(
                    text(
                        "INSERT INTO public.sub_party_context (tenant_id,party_id,status,data_classification) "
                        "VALUES (:tenant,:id,'active','test')"
                    ),
                    {"id": extra, "tenant": OPERATOR_TENANT_ID},
                )
        return result

    monkeypatch.setattr(adoption, stage_name, corrupting_stage)
    with engine.connect() as connection:
        root = connection.begin()
        before = entry.observe_fixture_plan(connection)
        rows = _rows(connection, "public")
        try:
            with pytest.raises(
                (PartyImportRefused, HistoricalOperatorTenantImportRefused),
                match="destination verification failed",
            ):
                adoption.rehearse_party_import(
                    connection, plan=before, cohort=_cohort(connection)
                )
            assert root.is_active
        finally:
            root.rollback()
        assert not root.is_active
        assert connection.get_transaction() is None
        with connection.begin() as verification:
            assert verification is not root
            assert connection.get_transaction() is verification
            assert entry.observe_fixture_plan(connection) == before
            assert _rows(connection, "public") == rows
            assert (
                connection.scalar(
                    text(
                        "SELECT to_regprocedure('public.party_import_corruption_fixture()')"
                    )
                )
                is None
            )


@pytest.mark.parametrize("denied_scope", ("other", "unset"))
def test_context_rls_under_temporary_nonprivileged_role(
    engine: Engine,
    exact_parties: None,
    denied_scope: str,
) -> None:
    from sqlalchemy.exc import DBAPIError

    role = "party_import_rls_fixture_" + uuid4().hex
    with engine.connect() as connection:
        root = connection.begin()
        before = entry.observe_fixture_plan(connection)
        rows = _rows(connection, "public")
        try:
            cohort = _cohort(connection)
            adoption.rehearse_party_import(connection, plan=before, cohort=cohort)
            # A valid canonical FK target with no context removes constraint
            # violations as alternative explanations for the refused write.
            target = uuid4()
            denied_target = uuid4()
            connection.execute(
                text(
                    "INSERT INTO public.parties (id,tenant_id,party_type,display_name,is_active,custom_fields,created_at,updated_at) "
                    "VALUES (:id,:tenant,'person','RLS fixture',true,'{}',now(),now())"
                ),
                {"id": target, "tenant": OPERATOR_TENANT_ID},
            )
            connection.execute(
                text(
                    "INSERT INTO public.parties (id,tenant_id,party_type,display_name,is_active,custom_fields,created_at,updated_at) "
                    "VALUES (:id,:tenant,'person','Denied RLS fixture',true,'{}',now(),now())"
                ),
                {"id": denied_target, "tenant": OPERATOR_TENANT_ID},
            )
            quote = connection.dialect.identifier_preparer.quote
            quoted_role = quote(role)
            connection.exec_driver_sql(
                f"CREATE ROLE {quoted_role} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT"
            )
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
            connection.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION public.app_current_tenant_id() TO {quoted_role}"
            )
            # These sacrificial grants exist only inside this fixture root.
            connection.exec_driver_sql(
                f"GRANT SELECT,INSERT ON public.sub_party_context TO {quoted_role}"
            )
            connection.exec_driver_sql(f"SET LOCAL ROLE {quoted_role}")
            assert connection.scalar(text("SELECT current_user")) == role
            assert connection.execute(
                text(
                    "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            ).one() == (False, False)
            connection.execute(text("SELECT set_config('row_security','on',true)"))
            connection.execute(
                text("SELECT set_config('app.current_tenant',:tenant,true)"),
                {"tenant": str(OPERATOR_TENANT_ID)},
            )
            assert connection.scalar(
                text("SELECT count(*) FROM public.sub_party_context")
            ) == len(cohort.dispositions)
            connection.execute(
                text(
                    "INSERT INTO public.sub_party_context (tenant_id,party_id,status,data_classification) "
                    "VALUES (:tenant,:party,'active','test')"
                ),
                {"tenant": OPERATOR_TENANT_ID, "party": target},
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM public.sub_party_context"))
                == len(cohort.dispositions) + 1
            )
            connection.execute(
                text("SELECT set_config('app.current_tenant',:tenant,true)"),
                {"tenant": str(uuid4())},
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM public.sub_party_context"))
                == 0
            )
            connection.execute(text("SELECT set_config('app.current_tenant','',true)"))
            assert (
                connection.scalar(text("SELECT count(*) FROM public.sub_party_context"))
                == 0
            )
            if denied_scope == "other":
                connection.execute(
                    text("SELECT set_config('app.current_tenant',:tenant,true)"),
                    {"tenant": str(uuid4())},
                )
            with pytest.raises(DBAPIError) as refused:
                connection.execute(
                    text(
                        "INSERT INTO public.sub_party_context (tenant_id,party_id,status,data_classification) "
                        "VALUES (:tenant,:party,'active','test')"
                    ),
                    {"tenant": OPERATOR_TENANT_ID, "party": denied_target},
                )
            assert getattr(refused.value.orig, "sqlstate", None) == "42501"
            assert "row-level security" in str(refused.value.orig).lower()
        finally:
            # Restores role, grants, function privileges, histories and all rows.
            root.rollback()
        assert not root.is_active
        assert connection.get_transaction() is None
        with connection.begin() as verification:
            assert verification is not root
            assert connection.get_transaction() is verification
            assert (
                connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                    {"role": role},
                )
                is False
            )
            assert entry.observe_fixture_plan(connection) == before
            assert _rows(connection, "public") == rows
