"""Migration runtime grants, planned without a database.

The migrator (app_admin, BYPASSRLS) owns what a migration creates; the runtime
role (dotmac_app) is granted DML on it before the transaction commits. It never
owns new objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.migration_ownership import (
    LEGACY_OWNER_ENV_VAR,
    RUNTIME_ROLE_ENV_VAR,
    MigrationOwnershipError,
    MigratorPosture,
    OwnedObject,
    assert_public_schema_posture,
    grant_runtime_privileges,
    load_definer_allowlist,
    migrator_violations,
    plan_runtime_grants,
    public_schema_findings,
    runtime_role_from_environ,
    unowned_bypass_violation,
)

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / "alembic" / "definer_owner_allowlist.txt"
RELAY_CLAIM = "public.claim_outbox_batch(text, integer, integer)"
ALLOWED = frozenset({RELAY_CLAIM})


def table(name: str, oid: int, kind: str = "r") -> OwnedObject:
    return OwnedObject("pg_class", oid, kind, f"public.{name}", f"public.{name}")


def index(name: str, oid: int) -> OwnedObject:
    return OwnedObject(
        "pg_class", oid, "i", f"public.{name}", f"public.{name}", follows_parent=True
    )


def func(
    name: str, oid: int, *, secdef: bool = False, signature: str | None = None
) -> OwnedObject:
    return OwnedObject(
        "pg_proc",
        oid,
        "f",
        f"public.{name}()",
        signature or f"public.{name}()",
        secdef=secdef,
    )


def plan(*objects: OwnedObject, allowlist: frozenset[str] = frozenset()):
    return plan_runtime_grants(objects, definer_allowlist=allowlist)


def stmts(result) -> tuple[str, ...]:
    return result.statements("dotmac_app")


DML = "SELECT, INSERT, UPDATE, DELETE"
SEQ = "USAGE, SELECT, UPDATE"


def test_new_table_gets_dml_and_its_index_follows() -> None:
    result = plan(table("invoices", 1), index("ix_invoices", 2))
    assert result.refusals == ()
    assert stmts(result) == (f'GRANT {DML} ON TABLE public.invoices TO "dotmac_app"',)


def test_no_plan_ever_transfers_ownership() -> None:
    result = plan(table("t", 1), table("s", 2, "S"), func("f", 3))
    assert all("OWNER TO" not in s for s in stmts(result))


def test_partitioned_table_and_partitions_are_each_granted() -> None:
    result = plan(table("events", 4, "p"), table("events_2026", 5))
    assert stmts(result) == (
        f'GRANT {DML} ON TABLE public.events TO "dotmac_app"',
        f'GRANT {DML} ON TABLE public.events_2026 TO "dotmac_app"',
    )


def test_security_invoker_view_gets_dml_like_a_table() -> None:
    view = OwnedObject(
        "pg_class", 7, "v", "public.v_open", "public.v_open", security_invoker=True
    )
    assert stmts(plan(view)) == (f'GRANT {DML} ON TABLE public.v_open TO "dotmac_app"',)


def test_view_without_security_invoker_is_refused() -> None:
    """An app_admin-owned view would read FORCE-RLS tables with owner bypass."""
    result = plan(table("v_tenant_totals", 7, "v"))
    assert result.items == ()
    assert len(result.refusals) == 1
    assert "security_invoker=true" in result.refusals[0]
    assert "public.v_tenant_totals" in result.refusals[0]


def test_materialized_view_foreign_table_and_composite_are_refused() -> None:
    result = plan(
        table("mv_totals", 2, "m"),
        table("ft_ext", 3, "f"),
        table("rowtype", 5, "c"),
    )
    assert result.items == ()
    assert len(result.refusals) == 3
    assert any("materialized view public.mv_totals" in r for r in result.refusals)


def test_standalone_and_column_owned_sequences_are_both_granted() -> None:
    owned_by_column = OwnedObject(
        "pg_class", 10, "S", "public.t_id_seq", "public.t_id_seq", follows_parent=True
    )
    standalone = table("invoice_number_seq", 11, "S")
    result = plan(owned_by_column, standalone)
    assert stmts(result) == (
        f'GRANT {SEQ} ON SEQUENCE public.invoice_number_seq TO "dotmac_app"',
        f'GRANT {SEQ} ON SEQUENCE public.t_id_seq TO "dotmac_app"',
    )


def test_enum_domain_range_types_need_no_grant_array_and_row_types_follow() -> None:
    objs = [
        OwnedObject("pg_type", i, kind, f"public.t_{kind}", f"public.t_{kind}")
        for i, kind in enumerate("erd", 1)
    ]
    objs.append(
        OwnedObject(
            "pg_type", 99, "b", "public._t_e", "public._t_e", follows_parent=True
        )
    )
    result = plan(*objs)
    assert result.refusals == () and result.items == ()


def test_base_and_pseudo_types_are_refused() -> None:
    base = OwnedObject("pg_type", 1, "b", "public.mytype", "public.mytype")
    assert len(plan(base).refusals) == 1


def test_plain_function_needs_no_grant() -> None:
    result = plan(func("helper", 5))
    assert result.items == () and result.refusals == ()


def test_unlisted_security_definer_is_refused() -> None:
    result = plan(func("sneaky", 6, secdef=True))
    assert result.items == ()
    assert result.refusals == (
        "SECURITY DEFINER function public.sneaky() is not in the definer allowlist",
    )


def test_listed_security_definer_is_accepted_and_stays_with_the_migrator() -> None:
    claim = func("claim_outbox_batch", 8, secdef=True, signature=RELAY_CLAIM)
    result = plan(claim, allowlist=ALLOWED)
    assert result.items == () and result.refusals == ()


def test_aggregates_and_unknown_catalogs_are_refused_not_skipped() -> None:
    aggregate = OwnedObject("pg_proc", 3, "a", "public.agg(int)", "public.agg(int)")
    schema = OwnedObject("pg_namespace", 4, "", "pg_namespace:4", "pg_namespace:4")
    composite = OwnedObject("pg_class", 5, "c", "public.rowtype", "public.rowtype")
    result = plan(aggregate, schema, composite)
    assert result.items == ()
    assert len(result.refusals) == 3


def test_extension_members_and_temp_objects_are_ignored() -> None:
    ext = OwnedObject(
        "pg_class", 1, "r", "public.spatial_ref_sys", "x", extension_member=True
    )
    temp = OwnedObject("pg_class", 2, "r", "pg_temp_3.scratch", "x", temporary=True)
    assert plan(ext, temp) == plan()


def test_table_privileges_can_be_narrowed_for_the_version_table() -> None:
    result = plan_runtime_grants(
        [table("alembic_version", 1)],
        definer_allowlist=frozenset(),
        table_privileges=("SELECT",),
    )
    assert stmts(result) == (
        'GRANT SELECT ON TABLE public.alembic_version TO "dotmac_app"',
    )


def test_identifiers_are_quoted_in_the_role_name() -> None:
    result = plan(table("a", 1))
    assert result.statements('odd"role') == (
        f'GRANT {DML} ON TABLE public.a TO "odd""role"',
    )


def test_shipped_allowlist_is_exactly_the_four_relay_definers() -> None:
    assert load_definer_allowlist(ALLOWLIST) == frozenset(
        {
            "public.claim_outbox_batch(text, integer, integer)",
            "public.settle_outbox_event(uuid, text, text, timestamp with time zone, "
            "integer, text)",
            "public.claim_platform_outbox_batch(text, integer, integer)",
            "public.settle_platform_outbox_event(uuid, text, text, "
            "timestamp with time zone, integer, text)",
        }
    )


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class FakeConnection:
    """Replays one snapshot, records GRANTs, answers the privilege proof.

    ``held`` are oids that already hold every privilege; everything else lacks
    them until a GRANT ran, after which ``still_missing`` (default none) is
    what the proof reports.
    """

    def __init__(
        self,
        snapshot: list[OwnedObject],
        *,
        held: set[int] | None = None,
        still_missing: list[str] | None = None,
    ):
        self.snapshot = snapshot
        self.held = held or set()
        self.still_missing = still_missing or []
        self.executed: list[str] = []
        self.granted: set[int] = set()

    def execute(self, statement: object, params: Any = None) -> _Rows:
        sql = str(statement)
        if "has_table_privilege" in sql or "has_sequence_privilege" in sql:
            if params["oid"] in self.held:
                return _Rows([])
            if params["oid"] not in self.granted:
                return _Rows([(p,) for p in params["privs"]])
            return _Rows([(p,) for p in self.still_missing])
        return _Rows(
            [
                (
                    o.catalog,
                    o.oid,
                    o.kind,
                    o.ident,
                    o.signature,
                    o.secdef,
                    o.follows_parent,
                    o.extension_member,
                    o.temporary,
                    o.security_invoker,
                )
                for o in self.snapshot
            ]
        )

    def exec_driver_sql(self, statement: str) -> None:
        self.executed.append(statement)
        for obj in self.snapshot:
            if f" {obj.ident} TO " in statement:
                self.granted.add(obj.oid)


def grant(conn: FakeConnection) -> int:
    return grant_runtime_privileges(
        conn, runtime_role="dotmac_app", definer_allowlist=frozenset()
    )


def test_grant_step_grants_what_is_missing_and_skips_what_is_held() -> None:
    conn = FakeConnection(
        [table("old", 1), table("new", 2), index("ix_new", 3)], held={1}
    )
    assert grant(conn) == 1
    assert conn.executed == [f'GRANT {DML} ON TABLE public.new TO "dotmac_app"']


def test_a_retry_after_a_partial_commit_grants_objects_from_the_earlier_run() -> None:
    """Objects committed by an earlier (failed) run are not 'new' to this one."""
    conn = FakeConnection([table("committed_earlier", 1), table("also_earlier", 2)])
    assert grant(conn) == 2


def test_version_table_is_only_ever_granted_select() -> None:
    conn = FakeConnection([table("alembic_version", 2)])
    grant(conn)
    assert conn.executed == [
        'GRANT SELECT ON TABLE public.alembic_version TO "dotmac_app"'
    ]


def test_refusal_aborts_before_any_grant() -> None:
    conn = FakeConnection([table("ok", 1), func("evil", 4, secdef=True)])
    with pytest.raises(MigrationOwnershipError, match="cannot be granted"):
        grant(conn)
    assert conn.executed == []


def test_an_existing_view_that_lost_security_invoker_is_refused() -> None:
    """CREATE OR REPLACE VIEW / ALTER VIEW RESET on an already-granted view."""
    weakened = OwnedObject(
        "pg_class", 9, "v", "public.v_old", "public.v_old", security_invoker=False
    )
    conn = FakeConnection([weakened], held={9})
    with pytest.raises(MigrationOwnershipError, match="security_invoker"):
        grant(conn)


def test_an_existing_function_made_security_definer_is_refused() -> None:
    conn = FakeConnection([func("was_plain", 5, secdef=True)])
    with pytest.raises(MigrationOwnershipError, match="not in the definer allowlist"):
        grant(conn)


def test_grant_step_raises_when_a_privilege_is_not_held_afterwards() -> None:
    conn = FakeConnection([table("stuck", 2)], still_missing=["DELETE"])
    with pytest.raises(MigrationOwnershipError, match="lacks planned privileges"):
        grant(conn)


def test_public_create_is_refused_for_the_migrator_and_only_reported_for_runtime() -> (
    None
):
    refusals, reports = public_schema_findings(
        migrator_can_create=False, runtime_can_create=False, runtime_role="dotmac_app"
    )
    assert len(refusals) == 1 and reports == ()
    refusals, reports = public_schema_findings(
        migrator_can_create=True, runtime_can_create=True, runtime_role="dotmac_app"
    )
    assert refusals == () and "dotmac_app holds CREATE" in reports[0]
    assert public_schema_findings(
        migrator_can_create=True, runtime_can_create=False, runtime_role="dotmac_app"
    ) == ((), ())


class _PostureConnection:
    def __init__(self, migrator: bool, runtime: bool) -> None:
        self.row = (migrator, runtime)

    def execute(self, _statement: object, _params: object = None):
        rows = [self.row]

        class _Result:
            def fetchall(self) -> list[tuple[bool, bool]]:
                return rows

        return _Result()


def test_posture_check_raises_without_migrator_create_and_reports_runtime_create() -> (
    None
):
    with pytest.raises(MigrationOwnershipError, match="lacks CREATE"):
        assert_public_schema_posture(
            _PostureConnection(False, False), runtime_role="dotmac_app"
        )
    reports = assert_public_schema_posture(
        _PostureConnection(True, True), runtime_role="dotmac_app"
    )
    assert len(reports) == 1


def test_runtime_role_prefers_the_new_name_and_accepts_the_legacy_one() -> None:
    assert runtime_role_from_environ({}) == ""
    assert (
        runtime_role_from_environ({RUNTIME_ROLE_ENV_VAR: "dotmac_app"}) == "dotmac_app"
    )
    assert (
        runtime_role_from_environ({LEGACY_OWNER_ENV_VAR: " dotmac_app "})
        == "dotmac_app"
    )
    both = {RUNTIME_ROLE_ENV_VAR: "dotmac_app", LEGACY_OWNER_ENV_VAR: "dotmac_app"}
    assert runtime_role_from_environ(both) == "dotmac_app"


def test_conflicting_runtime_role_settings_are_refused() -> None:
    with pytest.raises(MigrationOwnershipError, match="conflicts"):
        runtime_role_from_environ(
            {RUNTIME_ROLE_ENV_VAR: "a", LEGACY_OWNER_ENV_VAR: "b"}
        )


def good_posture(**overrides: Any) -> MigratorPosture:
    base: dict[str, Any] = {
        "current_user": "app_admin",
        "owner_exists": True,
        "bypassrls": True,
        "superuser": False,
        "inherits_owner": True,
        "can_set_owner": True,
        "owner_reaches_migrator": False,
    }
    base.update(overrides)
    return MigratorPosture(**base)


def test_the_contract_migrator_has_no_violations() -> None:
    assert migrator_violations(good_posture(), owner="dotmac_app") == ()


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"current_user": "dotmac_app"}, "connected as the object owner"),
        ({"superuser": True}, "is a superuser"),
        ({"bypassrls": False}, "lacks BYPASSRLS"),
        ({"inherits_owner": False}, "with INHERIT"),
        ({"can_set_owner": False}, "cannot SET ROLE dotmac_app"),
        ({"owner_reaches_migrator": True}, "must not reach the migrator"),
        ({"owner_exists": False}, "does not exist"),
    ],
)
def test_each_identity_violation_is_named(
    override: dict[str, Any], fragment: str
) -> None:
    problems = migrator_violations(good_posture(**override), owner="dotmac_app")
    assert any(fragment in problem for problem in problems), problems


ENV = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")


def test_env_sets_lock_timeout_before_the_alembic_version_alter() -> None:
    body = ENV[ENV.index("def run_migrations_online") :]
    assert body.index("_set_migration_lock_timeout(connection)") < body.index(
        "ensure_alembic_version_table(connection)"
    )


def test_env_alters_alembic_version_only_when_the_width_differs() -> None:
    ensure = ENV[
        ENV.index("def ensure_alembic_version_table") : ENV.index("def include_object")
    ]
    assert "atttypmod" in ensure
    assert "if typmod != _VERSION_NUM_TYPMOD" in ensure
    assert ensure.index("atttypmod") < ensure.index("ALTER TABLE alembic_version")


def test_env_grants_inside_the_migration_transaction_and_before_commit() -> None:
    body = ENV[ENV.index("def run_migrations_online") :]
    # Bootstrap table: granted before the intermediate commit.
    assert body.index("grant_runtime_privileges(") < body.index("connection.commit()")
    # Migrations: run, then grant, inside begin_transaction.
    inside = body[body.index("with context.begin_transaction():") :]
    assert inside.index("context.run_migrations()") < inside.index(
        "grant_runtime_privileges("
    )


def test_env_grant_is_opt_in_through_the_runtime_role_variable() -> None:
    assert "runtime_role_from_environ" in ENV
    assert "if owner is not None" in ENV


def test_bypassrls_non_superuser_without_owner_variable_is_refused() -> None:
    message = unowned_bypass_violation(
        current_user="app_admin", bypassrls=True, superuser=False
    )
    assert message is not None
    assert "MIGRATION_RUNTIME_ROLE" in message and "module_database_roles.v1" in message


def test_superuser_and_plain_roles_may_migrate_without_the_variable() -> None:
    for bypass, superuser in ((True, True), (False, False), (False, True)):
        assert (
            unowned_bypass_violation(
                current_user="x", bypassrls=bypass, superuser=superuser
            )
            is None
        )


def test_env_checks_unowned_bypass_before_any_ddl() -> None:
    body = ENV[ENV.index("def run_migrations_online") :]
    assert body.index("assert_owner_variable_set_for_bypass(connection)") < body.index(
        "ensure_alembic_version_table(connection)"
    )
