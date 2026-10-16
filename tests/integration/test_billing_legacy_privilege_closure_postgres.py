"""Hosted actual-login attachment closure on the real migrated private clone.

This component does not prove pre-existing protected FK/helper/view global closure.
The imported fixture owns the isolated CI cluster's role-maintenance lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.services.billing.legacy_archive_fence_contracts import (
    ArchiveFenceStagedObservation,
)
from app.services.billing.legacy_privilege_closure import (
    ClosurePopulation,
    PrivilegeClosureRefused,
)
from app.services.billing.legacy_retained_columns_contracts import (
    RetainedColumnsStagedObservation,
)
from tests.integration.test_billing_legacy_archive_fence_postgres import (
    FenceConnections,
    fence_connections,  # noqa: F401 -- shared isolated clone fixture
)
from tests.integration.test_billing_legacy_archive_fence_postgres import (
    _install as _archive_install,
)
from tests.integration.test_billing_legacy_retained_columns_postgres import (
    _install as _retained_install,
)
from tests.integration.test_billing_legacy_retained_columns_postgres import (
    retained_connections,  # noqa: F401 -- shared isolated clone fixture
)
from tests.integration.test_billing_runtime_login_postgres import (
    runtime_connections,  # noqa: F401 -- transitive migrated actual-login fixture
)

pytestmark = pytest.mark.integration
_ROLES = ("app_user", "platform_api", "outbox_dispatcher", "platform_outbox_dispatcher")
_OUTBOX = ("outbox_events", "platform_outbox_events")


def _install(connections: FenceConnections, population: ClosurePopulation) -> None:
    if population is ClosurePopulation.ARCHIVE:
        _archive_install(connections.owner)
    else:
        _retained_install(connections.owner)


@contextmanager
def _login(connections: FenceConnections, role: str) -> Iterator[psycopg.Connection]:
    if role == "app_user":
        yield connections.runtime
        return
    admin = connections.admin
    # Never fetch a password verifier. Restore only the proven NULL baseline.
    assert admin.execute(
        "SELECT rolpassword IS NULL FROM pg_authid WHERE rolname=%s", (role,)
    ).fetchone() == (True,)
    password = uuid4().hex
    admin.execute(
        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
    try:
        with psycopg.connect(
            host=admin.info.host,
            port=admin.info.port,
            dbname=admin.info.dbname,
            user=role,
            password=password,
        ) as connection:
            assert connection.execute(
                "SELECT session_user,current_user"
            ).fetchone() == (role, role)
            connection.rollback()
            yield connection
    finally:
        admin.execute(
            sql.SQL("ALTER ROLE {} PASSWORD NULL").format(sql.Identifier(role))
        )


def _acl(admin: psycopg.Connection) -> list[tuple[object, ...]]:
    return admin.execute("""SELECT c.oid, c.relacl::text,
        ARRAY(SELECT a.attacl::text FROM pg_attribute a WHERE a.attrelid=c.oid
              AND a.attnum>0 ORDER BY a.attnum)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.oid""").fetchall()


def _no_install(admin: psycopg.Connection) -> None:
    assert (
        admin.execute("""SELECT count(*) FROM pg_trigger
        WHERE tgname LIKE 'dotmac_billing_archive_%' OR tgname LIKE 'dotmac_retained_%'""").fetchone()
        == (0,)
    )
    assert (
        admin.execute("""SELECT count(*) FROM event_store
        WHERE event_type IN ('billing.legacy_archive_fence.staged','billing.legacy_retained_columns.staged')""").fetchone()
        == (0,)
    )


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
def test_actual_logins_lose_trigger_table_and_column_references_only(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
) -> None:
    connections = retained_connections
    admin = connections.admin
    protected = "invoices" if population is ClosurePopulation.ARCHIVE else "subscribers"
    targets = (protected, *_OUTBOX)
    admin.execute("CREATE SCHEMA attachment_canary")
    admin.execute("""CREATE FUNCTION attachment_canary.noop() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog
        AS $$ BEGIN RETURN NEW; END $$""")
    for role in _ROLES:
        admin.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA attachment_canary TO {}").format(
                sql.Identifier(role)
            )
        )
        for name in targets:
            admin.execute(
                sql.SQL(
                    "GRANT TRIGGER, REFERENCES ON public.{} TO {} WITH GRANT OPTION"
                ).format(sql.Identifier(name), sql.Identifier(role))
            )
            admin.execute(
                sql.SQL(
                    "GRANT REFERENCES(id) ON public.{} TO {} WITH GRANT OPTION"
                ).format(sql.Identifier(name), sql.Identifier(role))
            )
    admin.execute(
        sql.SQL("GRANT TRIGGER, REFERENCES ON public.{} TO PUBLIC").format(
            sql.Identifier(protected)
        )
    )
    admin.execute(
        sql.SQL("GRANT REFERENCES(id) ON public.{} TO PUBLIC").format(
            sql.Identifier(protected)
        )
    )
    # Actual authenticated construction succeeds before closure. Rollback removes
    # each attachment and its runtime-owned referencing table before observation.
    for role in _ROLES:
        with _login(connections, role) as runtime:
            for name in targets:
                runtime.execute(
                    sql.SQL(
                        "CREATE TRIGGER attachment_canary BEFORE UPDATE ON public.{} FOR EACH ROW EXECUTE FUNCTION attachment_canary.noop()"
                    ).format(sql.Identifier(name))
                )
                runtime.execute(
                    sql.SQL(
                        "CREATE TABLE attachment_canary.child(id uuid REFERENCES public.{}(id))"
                    ).format(sql.Identifier(name))
                )
                runtime.rollback()
    before_dml = admin.execute("""SELECT c.relname,a.grantee,a.privilege_type,a.is_grantable
        FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE c.oid=ANY(ARRAY['public.outbox_events'::regclass,'public.platform_outbox_events'::regclass])
        AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE') ORDER BY 1,2,3""").fetchall()
    _install(connections, population)
    _install(connections, population)
    assert (
        before_dml
        == admin.execute("""SELECT c.relname,a.grantee,a.privilege_type,a.is_grantable
        FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE c.oid=ANY(ARRAY['public.outbox_events'::regclass,'public.platform_outbox_events'::regclass])
        AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE') ORDER BY 1,2,3""").fetchall()
    )
    for role in _ROLES:
        with _login(connections, role) as runtime:
            for name in targets:
                for statement in (
                    sql.SQL(
                        "CREATE TRIGGER attachment_canary BEFORE UPDATE ON public.{} FOR EACH ROW EXECUTE FUNCTION attachment_canary.noop()"
                    ).format(sql.Identifier(name)),
                    sql.SQL(
                        "CREATE TABLE attachment_canary.child(id uuid REFERENCES public.{}(id))"
                    ).format(sql.Identifier(name)),
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        runtime.execute(statement)
                    runtime.rollback()
                assert admin.execute(
                    "SELECT has_table_privilege(%s,%s,'TRIGGER'),has_table_privilege(%s,%s,'REFERENCES'),has_any_column_privilege(%s,%s,'REFERENCES')",
                    (
                        role,
                        "public." + name,
                        role,
                        "public." + name,
                        role,
                        "public." + name,
                    ),
                ).fetchone() == (False, False, False)
    assert admin.execute(
        "SELECT count(*) FROM pg_roles WHERE rolname='dotmac_public_probe'"
    ).fetchone() == (0,)


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
def test_malicious_invoker_trigger_borrows_definer_and_installer_refuses(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
) -> None:
    connections = retained_connections
    admin, runtime = connections.admin, connections.runtime
    admin.execute("CREATE TABLE public.definer_escalation_canary(actor name)")
    admin.execute("GRANT INSERT ON public.definer_escalation_canary TO app_admin")
    admin.execute("""CREATE FUNCTION public.definer_escalation_trigger() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
        BEGIN INSERT INTO public.definer_escalation_canary VALUES(current_user); RETURN NEW; END $$""")
    admin.execute("GRANT TRIGGER ON public.platform_outbox_events TO app_user")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        runtime.execute(
            "INSERT INTO public.definer_escalation_canary VALUES(current_user)"
        )
    runtime.rollback()
    runtime.execute("""CREATE TRIGGER definer_escalation BEFORE UPDATE ON public.platform_outbox_events
        FOR EACH ROW EXECUTE FUNCTION public.definer_escalation_trigger()""")
    runtime.commit()
    identity = uuid4()
    admin.execute(
        "INSERT INTO public.platform_outbox_events(id,event_type,available_at) VALUES(%s,'closure.canary','-infinity')",
        (identity,),
    )
    with _login(connections, "platform_outbox_dispatcher") as dispatcher:
        assert dispatcher.execute(
            "SELECT id FROM public.claim_platform_outbox_batch('escalation',1,60)"
        ).fetchone() == (identity,)
        dispatcher.commit()
    assert admin.execute(
        "SELECT actor FROM public.definer_escalation_canary"
    ).fetchall() == [("app_admin",)]
    before = _acl(admin)
    with pytest.raises(PrivilegeClosureRefused):
        _install(connections, population)
    assert before == _acl(admin)
    _no_install(admin)
    # Refusal never silently repairs the hostile trigger.
    assert admin.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgname='definer_escalation'"
    ).fetchone() == (1,)


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
def test_all_four_exact_helpers_still_claim_and_settle(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
) -> None:
    connections = retained_connections
    admin = connections.admin
    tenant = admin.execute(
        "SELECT id FROM public.tenants ORDER BY id LIMIT 1"
    ).fetchone()[0]
    identities = (uuid4(), uuid4())
    admin.execute(
        "INSERT INTO public.outbox_events(id,tenant_id,event_type,available_at) VALUES(%s,%s,'closure.canary','-infinity')",
        (identities[0], tenant),
    )
    admin.execute(
        "INSERT INTO public.platform_outbox_events(id,event_type,available_at) VALUES(%s,'closure.canary','-infinity')",
        (identities[1],),
    )
    _install(connections, population)
    for role, prefix, identity in (
        ("outbox_dispatcher", "", identities[0]),
        ("platform_outbox_dispatcher", "platform_", identities[1]),
    ):
        with _login(connections, role) as dispatcher:
            assert dispatcher.execute(
                sql.SQL("SELECT id FROM public.{}('closure',1,60)").format(
                    sql.Identifier("claim_" + prefix + "outbox_batch")
                )
            ).fetchone() == (identity,)
            assert dispatcher.execute(
                sql.SQL("SELECT public.{}(%s,'closure','sent',NULL,1,NULL)").format(
                    sql.Identifier("settle_" + prefix + "outbox_event")
                ),
                (identity,),
            ).fetchone() == (True,)
            dispatcher.commit()
    assert admin.execute(
        "SELECT status,attempts,leased_by FROM public.outbox_events WHERE id=%s",
        (identities[0],),
    ).fetchone() == ("sent", 1, None)
    assert admin.execute(
        "SELECT status,attempts,leased_by FROM public.platform_outbox_events WHERE id=%s",
        (identities[1],),
    ).fetchone() == ("sent", 1, None)
    _install(connections, population)


@pytest.mark.parametrize(
    "drift",
    (
        "ALTER FUNCTION public.claim_outbox_batch(text,integer,integer) SET search_path=public",
        "ALTER FUNCTION public.claim_outbox_batch(text,integer,integer) SECURITY INVOKER",
        "GRANT EXECUTE ON FUNCTION public.claim_outbox_batch(text,integer,integer) TO PUBLIC",
        "GRANT EXECUTE ON FUNCTION public.claim_outbox_batch(text,integer,integer) TO app_user",
        "GRANT EXECUTE ON FUNCTION public.claim_outbox_batch(text,integer,integer) TO outbox_dispatcher WITH GRANT OPTION",
        "CREATE FUNCTION public.claim_outbox_batch(integer) RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "CREATE OR REPLACE FUNCTION public.claim_outbox_batch(p_worker text,p_batch integer,p_stale_seconds integer) RETURNS SETOF public.outbox_events LANGUAGE sql SECURITY DEFINER SET search_path='' AS 'SELECT * FROM public.outbox_events'",
        "CREATE RULE unexpected_rule AS ON UPDATE TO public.platform_outbox_events DO ALSO NOTIFY closure_canary",
        "ALTER TABLE public.platform_outbox_events ADD COLUMN unknown_column integer",
        "ALTER TABLE public.outbox_events DROP CONSTRAINT fk_outbox_events_tenant",
        "ALTER TABLE public.outbox_events NO FORCE ROW LEVEL SECURITY",
        "ALTER POLICY outbox_events_tenant_isolation ON public.outbox_events USING(true)",
    ),
)
@pytest.mark.parametrize("replay", (False, True))
def test_helper_and_topology_drift_refuses_without_repair_or_second_audit(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    drift: str,
    replay: bool,
) -> None:
    connections = retained_connections
    if replay:
        _archive_install(connections.owner)
    connections.admin.execute(drift)
    before = _acl(connections.admin)
    with pytest.raises(PrivilegeClosureRefused):
        _archive_install(connections.owner)
    assert before == _acl(connections.admin)
    assert connections.admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_archive_fence.staged'"
    ).fetchone() == (int(replay),)
    if not replay:
        _no_install(connections.admin)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize(
    "options",
    (
        "INHERIT TRUE, SET FALSE, ADMIN FALSE",
        "INHERIT FALSE, SET TRUE, ADMIN FALSE",
        "INHERIT FALSE, SET FALSE, ADMIN FALSE",
    ),
)
def test_online_role_paths_including_dormant_refuse(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    role: str,
    options: str,
) -> None:
    connections = retained_connections
    parent = sql.Identifier("closure_parent_" + uuid4().hex)
    connections.admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(parent))
    try:
        connections.admin.execute(
            sql.SQL("GRANT {} TO {} WITH " + options).format(
                parent, sql.Identifier(role)
            )
        )
        with pytest.raises(PrivilegeClosureRefused):
            _archive_install(connections.owner)
        _no_install(connections.admin)
    finally:
        connections.admin.execute(
            sql.SQL("REVOKE {} FROM {}").format(parent, sql.Identifier(role))
        )
        connections.admin.execute(sql.SQL("DROP ROLE {}").format(parent))


def test_nonowner_grant_chain_refuses_and_retains_all_grants(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
) -> None:
    connections = retained_connections
    admin = connections.admin
    admin.execute("GRANT REFERENCES ON public.invoices TO app_user WITH GRANT OPTION")
    connections.runtime.execute("GRANT REFERENCES ON public.invoices TO platform_api")
    connections.runtime.commit()
    before = _acl(admin)
    with pytest.raises(PrivilegeClosureRefused):
        _archive_install(connections.owner)
    assert before == _acl(admin)
    _no_install(admin)


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
def test_attachment_revokes_roll_back_with_owner_failure(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
) -> None:
    from sqlalchemy.orm import Session

    from app.services.owner_commands import CommandContext, execute_owner_command
    from tests.integration.test_billing_legacy_archive_fence_postgres import (
        _DEFINITION,
    )
    from tests.integration.test_billing_legacy_archive_fence_postgres import (
        _stage as _archive_stage,
    )
    from tests.integration.test_billing_legacy_retained_columns_postgres import (
        _stage as _retained_stage,
    )

    class InjectedFailure(Exception):
        pass

    connections = retained_connections
    protected = "invoices" if population is ClosurePopulation.ARCHIVE else "subscribers"
    for name in (protected, *_OUTBOX):
        connections.admin.execute(
            sql.SQL(
                "GRANT TRIGGER, REFERENCES ON public.{} TO app_user WITH GRANT OPTION"
            ).format(sql.Identifier(name))
        )
        connections.admin.execute(
            sql.SQL("GRANT REFERENCES(id) ON public.{} TO PUBLIC").format(
                sql.Identifier(name)
            )
        )
    before = _acl(connections.admin)
    with Session(connections.owner) as db:

        def operation() -> None:
            if population is ClosurePopulation.ARCHIVE:
                _archive_stage(db)
            else:
                _retained_stage(db)
            raise InjectedFailure

        with pytest.raises(InjectedFailure):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary",
                    scope="clone",
                    reason="attachment rollback",
                    idempotency_key="attachment-rollback-v1",
                ),
                operation=operation,
            )
    assert before == _acl(connections.admin)
    _no_install(connections.admin)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize(
    "drift", ("elevated", "ownership", "schema_create", "database_create")
)
def test_online_elevation_ownership_and_create_capabilities_refuse(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    role: str,
    drift: str,
) -> None:
    connections = retained_connections
    admin = connections.admin
    identity = sql.Identifier(role)
    try:
        if drift == "elevated":
            admin.execute(sql.SQL("ALTER ROLE {} BYPASSRLS").format(identity))
        elif drift == "ownership":
            admin.execute("CREATE TABLE public.closure_runtime_owned(id integer)")
            admin.execute(
                sql.SQL("ALTER TABLE public.closure_runtime_owned OWNER TO {}").format(
                    identity
                )
            )
        elif drift == "schema_create":
            admin.execute(
                sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(identity)
            )
        else:
            admin.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(admin.info.dbname), identity
                )
            )
        with pytest.raises(PrivilegeClosureRefused):
            _archive_install(connections.owner)
        _no_install(admin)
    finally:
        if drift == "elevated":
            admin.execute(sql.SQL("ALTER ROLE {} NOBYPASSRLS").format(identity))
        elif drift == "ownership":
            admin.execute("DROP TABLE public.closure_runtime_owned")
        elif drift == "schema_create":
            admin.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA public FROM {} RESTRICT").format(
                    identity
                )
            )
        else:
            admin.execute(
                sql.SQL("REVOKE CREATE ON DATABASE {} FROM {} RESTRICT").format(
                    sql.Identifier(admin.info.dbname), identity
                )
            )


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
@pytest.mark.parametrize("privilege", ("TRIGGER", "REFERENCES", "REFERENCES(id)"))
def test_replayed_attachment_grant_drift_refuses_and_preserves_single_audit(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
    privilege: str,
) -> None:
    connections = retained_connections
    _install(connections, population)
    connections.admin.execute(
        sql.SQL("GRANT " + privilege + " ON public.platform_outbox_events TO PUBLIC")
    )
    before = _acl(connections.admin)
    with pytest.raises(PrivilegeClosureRefused):
        _install(connections, population)
    assert before == _acl(connections.admin)
    assert connections.admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type IN ('billing.legacy_archive_fence.staged','billing.legacy_retained_columns.staged')"
    ).fetchone() == (1,)


def test_temporary_outbox_shadow_cannot_change_public_helper_replay_digest(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
) -> None:
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.services.owner_commands import CommandContext, execute_owner_command
    from tests.integration.test_billing_legacy_archive_fence_postgres import _DEFINITION
    from tests.integration.test_billing_legacy_archive_fence_postgres import (
        _stage as _archive_stage,
    )

    with Session(retained_connections.owner) as db:

        def operation() -> ArchiveFenceStagedObservation:
            db.execute(text("CREATE TEMP TABLE outbox_events(id uuid)"))
            observation = _archive_stage(db)
            db.execute(text("DROP TABLE pg_temp.outbox_events"))
            return observation

        first = execute_owner_command(
            db,
            definition=_DEFINITION,
            context=CommandContext.system(
                actor="canary",
                scope="clone",
                reason="public helper deparser independence",
                idempotency_key="temporary-relay-shadow-v1",
            ),
            operation=operation,
        )
    replay = _archive_install(retained_connections.owner)
    assert not first.replay_observed and replay.replay_observed
    assert first.catalog_digest == replay.catalog_digest
    assert retained_connections.admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_archive_fence.staged'"
    ).fetchone() == (1,)


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
@pytest.mark.parametrize("replay", (False, True))
@pytest.mark.parametrize(
    ("relation", "privilege"),
    (
        (None, "BYPASSRLS"),
        ("outbox_events", "SELECT"),
        ("outbox_events", "UPDATE"),
        ("platform_outbox_events", "SELECT"),
        ("platform_outbox_events", "UPDATE"),
    ),
)
def test_definer_lost_capability_refuses_before_mutation_without_repair(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
    replay: bool,
    relation: str | None,
    privilege: str,
) -> None:
    from sqlalchemy import event

    connections = retained_connections
    admin = connections.admin
    if replay:
        _install(connections, population)
    # app_admin is cluster-wide; the imported fixture holds the maintenance
    # lock. Prove its exact flag baseline and restore only our own mutation.
    assert admin.execute(
        "SELECT rolbypassrls,rolsuper FROM pg_roles WHERE rolname='app_admin'"
    ).fetchone() == (True, False)
    flag_mutated = False
    statements: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    try:
        if relation is None:
            admin.execute("ALTER ROLE app_admin NOBYPASSRLS")
            flag_mutated = True
        else:
            assert admin.execute(
                "SELECT has_table_privilege('app_admin',%s,%s)",
                ("public." + relation, privilege),
            ).fetchone() == (True,)
            admin.execute(
                sql.SQL(
                    "REVOKE " + privilege + " ON public.{} FROM app_admin RESTRICT"
                ).format(sql.Identifier(relation))
            )
            assert admin.execute(
                "SELECT has_table_privilege('app_admin',%s,%s)",
                ("public." + relation, privilege),
            ).fetchone() == (False,)
        before_acl = _acl(admin)
        before_guards = admin.execute("""SELECT t.oid,pg_get_triggerdef(t.oid),t.tgenabled
            FROM pg_trigger t WHERE tgname LIKE 'dotmac_billing_archive_%'
            OR tgname LIKE 'dotmac_retained_%' ORDER BY t.oid""").fetchall()
        event.listen(connections.owner, "before_cursor_execute", capture)
        try:
            with pytest.raises(PrivilegeClosureRefused):
                _install(connections, population)
        finally:
            event.remove(connections.owner, "before_cursor_execute", capture)
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(
                (
                    "CREATE ",
                    "ALTER ",
                    "REVOKE ",
                    "GRANT ",
                    "INSERT ",
                    "UPDATE ",
                    "DELETE ",
                    "TRUNCATE ",
                )
            )
            for statement in statements
        )
        assert before_acl == _acl(admin)
        assert (
            before_guards
            == admin.execute("""SELECT t.oid,pg_get_triggerdef(t.oid),t.tgenabled
            FROM pg_trigger t WHERE tgname LIKE 'dotmac_billing_archive_%'
            OR tgname LIKE 'dotmac_retained_%' ORDER BY t.oid""").fetchall()
        )
        assert admin.execute(
            "SELECT count(*) FROM event_store WHERE event_type IN ('billing.legacy_archive_fence.staged','billing.legacy_retained_columns.staged')"
        ).fetchone() == (int(replay),)
        if relation is None:
            assert admin.execute(
                "SELECT rolbypassrls FROM pg_roles WHERE rolname='app_admin'"
            ).fetchone() == (False,)
        else:
            assert admin.execute(
                "SELECT has_table_privilege('app_admin',%s,%s)",
                ("public." + relation, privilege),
            ).fetchone() == (False,)
        if not replay:
            _no_install(admin)
    finally:
        if flag_mutated:
            admin.execute("ALTER ROLE app_admin BYPASSRLS")
            assert admin.execute(
                "SELECT rolbypassrls,rolsuper FROM pg_roles WHERE rolname='app_admin'"
            ).fetchone() == (True, False)
        # Revoked table ACLs belong only to the private clone and intentionally
        # remain revoked until clone teardown; no repair grant masks refusal.


def _policy_shadow_schema(connections: FenceConnections) -> None:
    connections.admin.execute("CREATE SCHEMA policy_shadow")
    connections.admin.execute("""CREATE FUNCTION policy_shadow.app_current_tenant_id()
        RETURNS uuid LANGUAGE sql SECURITY INVOKER AS 'SELECT NULL::uuid'""")
    connections.admin.execute(
        sql.SQL("GRANT USAGE ON SCHEMA policy_shadow TO {}").format(
            sql.Identifier(connections.owner.url.username)
        )
    )


def _install_with_shadow_search_path(
    connections: FenceConnections,
    population: ClosurePopulation,
    *,
    shadow_policy: bool,
    shadow_operator: bool = False,
) -> ArchiveFenceStagedObservation | RetainedColumnsStagedObservation:
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.services.owner_commands import CommandContext, execute_owner_command
    from tests.integration.test_billing_legacy_archive_fence_postgres import _DEFINITION
    from tests.integration.test_billing_legacy_archive_fence_postgres import (
        _stage as _archive_stage,
    )
    from tests.integration.test_billing_legacy_retained_columns_postgres import (
        _stage as _retained_stage,
    )

    with Session(connections.owner) as db:

        def operation() -> (
            ArchiveFenceStagedObservation | RetainedColumnsStagedObservation
        ):
            db.execute(
                text(
                    "SET LOCAL search_path TO operator_shadow, public, pg_catalog"
                    if shadow_operator
                    else "SET LOCAL search_path TO policy_shadow, public"
                )
            )
            rendered = db.execute(
                text("""SELECT pg_catalog.pg_get_expr(polqual,polrelid),
                pg_catalog.pg_get_expr(polwithcheck,polrelid)
                FROM pg_catalog.pg_policy WHERE polrelid='public.outbox_events'::regclass""")
            ).one()
            expected = (
                "(tenant_id = app_current_tenant_id())"
                if shadow_policy or shadow_operator
                else "(tenant_id = public.app_current_tenant_id())"
            )
            assert tuple(rendered) == (expected, expected)
            if population is ClosurePopulation.ARCHIVE:
                return _archive_stage(db)
            return _retained_stage(db)

        return execute_owner_command(
            db,
            definition=_DEFINITION,
            context=CommandContext.system(
                actor="canary",
                scope="clone",
                reason="policy resolved helper identity",
                idempotency_key="policy-shadow-v1",
            ),
            operation=operation,
        )


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
def test_public_policy_helper_digest_survives_earlier_schema_shadow(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
) -> None:
    connections = retained_connections
    _policy_shadow_schema(connections)
    before = connections.admin.execute(
        "SELECT oid,polqual::text,polwithcheck::text FROM pg_policy WHERE polrelid='public.outbox_events'::regclass"
    ).fetchall()
    first = _install_with_shadow_search_path(
        connections, population, shadow_policy=False
    )
    replay = (
        _archive_install(connections.owner)
        if population is ClosurePopulation.ARCHIVE
        else _retained_install(connections.owner)
    )
    assert not first.replay_observed and replay.replay_observed
    assert first.catalog_digest == replay.catalog_digest
    assert (
        before
        == connections.admin.execute(
            "SELECT oid,polqual::text,polwithcheck::text FROM pg_policy WHERE polrelid='public.outbox_events'::regclass"
        ).fetchall()
    )
    assert connections.admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type IN ('billing.legacy_archive_fence.staged','billing.legacy_retained_columns.staged')"
    ).fetchone() == (1,)


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
@pytest.mark.parametrize("replay", (False, True))
def test_actual_shadow_policy_helper_refuses_before_mutation_despite_same_rendering(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
    replay: bool,
) -> None:
    from sqlalchemy import event

    connections = retained_connections
    admin = connections.admin
    if replay:
        _install(connections, population)
    _policy_shadow_schema(connections)
    admin.execute("""ALTER POLICY outbox_events_tenant_isolation ON public.outbox_events
        USING(tenant_id=policy_shadow.app_current_tenant_id())
        WITH CHECK(tenant_id=policy_shadow.app_current_tenant_id())""")
    assert (
        admin.execute("""SELECT EXISTS(SELECT 1 FROM pg_depend d
        JOIN pg_policy p ON p.oid=d.objid
        WHERE d.classid='pg_policy'::regclass AND d.refclassid='pg_proc'::regclass
        AND p.polrelid='public.outbox_events'::regclass
        AND d.refobjid='policy_shadow.app_current_tenant_id()'::regprocedure),
        EXISTS(SELECT 1 FROM pg_depend d JOIN pg_policy p ON p.oid=d.objid
        WHERE d.classid='pg_policy'::regclass AND d.refclassid='pg_proc'::regclass
        AND p.polrelid='public.outbox_events'::regclass
        AND d.refobjid='public.app_current_tenant_id()'::regprocedure)""").fetchone()
        == (True, False)
    )
    before_acl = _acl(admin)
    before_policy = admin.execute(
        "SELECT oid,polqual::text,polwithcheck::text FROM pg_policy WHERE polrelid='public.outbox_events'::regclass"
    ).fetchall()
    before_guards = admin.execute("""SELECT oid,pg_get_triggerdef(oid),tgenabled FROM pg_trigger
        WHERE tgname LIKE 'dotmac_billing_archive_%' OR tgname LIKE 'dotmac_retained_%'
        ORDER BY oid""").fetchall()
    statements: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(connections.owner, "before_cursor_execute", capture)
    try:
        with pytest.raises(PrivilegeClosureRefused):
            _install_with_shadow_search_path(
                connections, population, shadow_policy=True
            )
    finally:
        event.remove(connections.owner, "before_cursor_execute", capture)
    assert not any(
        statement.lstrip()
        .upper()
        .startswith(
            (
                "CREATE ",
                "ALTER ",
                "REVOKE ",
                "GRANT ",
                "INSERT ",
                "UPDATE ",
                "DELETE ",
                "TRUNCATE ",
            )
        )
        for statement in statements
    )
    assert before_acl == _acl(admin)
    assert (
        before_policy
        == admin.execute(
            "SELECT oid,polqual::text,polwithcheck::text FROM pg_policy WHERE polrelid='public.outbox_events'::regclass"
        ).fetchall()
    )
    assert (
        before_guards
        == admin.execute("""SELECT oid,pg_get_triggerdef(oid),tgenabled FROM pg_trigger
        WHERE tgname LIKE 'dotmac_billing_archive_%' OR tgname LIKE 'dotmac_retained_%'
        ORDER BY oid""").fetchall()
    )
    assert admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type IN ('billing.legacy_archive_fence.staged','billing.legacy_retained_columns.staged')"
    ).fetchone() == (int(replay),)
    if not replay:
        _no_install(admin)


@pytest.mark.parametrize("population", tuple(ClosurePopulation))
@pytest.mark.parametrize("replay", (False, True))
def test_custom_uuid_equality_policy_refuses_despite_public_helper_and_same_rendering(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
    population: ClosurePopulation,
    replay: bool,
) -> None:
    from sqlalchemy import event

    connections = retained_connections
    admin = connections.admin
    if replay:
        _install(connections, population)
    admin.execute("CREATE SCHEMA operator_shadow")
    admin.execute("""CREATE FUNCTION operator_shadow.uuid_equal(uuid,uuid)
        RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY INVOKER AS 'SELECT true'""")
    admin.execute("""CREATE OPERATOR operator_shadow.=
        (LEFTARG=uuid, RIGHTARG=uuid, PROCEDURE=operator_shadow.uuid_equal)""")
    admin.execute(
        sql.SQL("GRANT USAGE ON SCHEMA operator_shadow TO {}").format(
            sql.Identifier(connections.owner.url.username)
        )
    )
    assert admin.execute(
        "SELECT NULL::uuid OPERATOR(operator_shadow.=) NULL::uuid"
    ).fetchone() == (True,)
    admin.execute("""ALTER POLICY outbox_events_tenant_isolation ON public.outbox_events
        USING(tenant_id OPERATOR(operator_shadow.=) public.app_current_tenant_id())
        WITH CHECK(tenant_id OPERATOR(operator_shadow.=) public.app_current_tenant_id())""")
    assert (
        admin.execute("""SELECT EXISTS(SELECT 1 FROM pg_depend d
        JOIN pg_policy p ON p.oid=d.objid
        WHERE d.classid='pg_policy'::regclass AND d.refclassid='pg_operator'::regclass
        AND p.polrelid='public.outbox_events'::regclass
        AND d.refobjid='operator_shadow.=(uuid,uuid)'::regoperator),
        EXISTS(SELECT 1 FROM pg_depend d JOIN pg_policy p ON p.oid=d.objid
        WHERE d.classid='pg_policy'::regclass AND d.refclassid='pg_proc'::regclass
        AND p.polrelid='public.outbox_events'::regclass
        AND d.refobjid='public.app_current_tenant_id()'::regprocedure)""").fetchone()
        == (True, True)
    )
    before_acl = _acl(admin)
    before_policy = admin.execute(
        "SELECT oid,polqual::text,polwithcheck::text FROM pg_policy WHERE polrelid='public.outbox_events'::regclass"
    ).fetchall()
    before_guards = admin.execute("""SELECT oid,pg_get_triggerdef(oid),tgenabled FROM pg_trigger
        WHERE tgname LIKE 'dotmac_billing_archive_%' OR tgname LIKE 'dotmac_retained_%'
        ORDER BY oid""").fetchall()
    statements: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(connections.owner, "before_cursor_execute", capture)
    try:
        with pytest.raises(PrivilegeClosureRefused):
            _install_with_shadow_search_path(
                connections, population, shadow_policy=False, shadow_operator=True
            )
    finally:
        event.remove(connections.owner, "before_cursor_execute", capture)
    assert not any(
        statement.lstrip()
        .upper()
        .startswith(
            (
                "CREATE ",
                "ALTER ",
                "REVOKE ",
                "GRANT ",
                "INSERT ",
                "UPDATE ",
                "DELETE ",
                "TRUNCATE ",
            )
        )
        for statement in statements
    )
    assert before_acl == _acl(admin)
    assert (
        before_policy
        == admin.execute(
            "SELECT oid,polqual::text,polwithcheck::text FROM pg_policy WHERE polrelid='public.outbox_events'::regclass"
        ).fetchall()
    )
    assert (
        before_guards
        == admin.execute("""SELECT oid,pg_get_triggerdef(oid),tgenabled FROM pg_trigger
        WHERE tgname LIKE 'dotmac_billing_archive_%' OR tgname LIKE 'dotmac_retained_%'
        ORDER BY oid""").fetchall()
    )
    assert admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type IN ('billing.legacy_archive_fence.staged','billing.legacy_retained_columns.staged')"
    ).fetchone() == (int(replay),)
    if not replay:
        _no_install(admin)
